from absl import flags
import pickle
import rospy
from std_msgs.msg import String
import time
import threading

import carla

from srunner.challenge.autoagents.autonomous_agent import AutonomousAgent, Track

from erdos.data_stream import DataStream
import erdos.graph
from erdos.message import Message, WatermarkMessage
from erdos.operators import NoopOp
from erdos.ros.ros_output_data_stream import ROSOutputDataStream
from erdos.timestamp import Timestamp

import config
from control.pid_control_operator import PIDControlOperator
from control.lidar_erdos_agent_operator import LidarERDOSAgentOperator
import operator_creator
from planning.challenge_planning_operator import ChallengePlanningOperator
import pylot_utils
import simulation.messages
import simulation.utils


FLAGS = flags.FLAGS
CENTER_CAMERA_NAME = 'front_center_camera'
LEFT_CAMERA_NAME = 'front_left_camera'
RIGHT_CAMERA_NAME = 'front_right_camera'


def add_visualization_operators(graph, rgb_camera_name):
    visualization_ops = []
    if FLAGS.visualize_rgb_camera:
        camera_video_op = operator_creator.create_camera_video_op(
            graph, rgb_camera_name, rgb_camera_name)
        visualization_ops.append(camera_video_op)
    if FLAGS.visualize_segmentation:
        # Segmented camera. The stream comes from CARLA.
        segmented_video_op = operator_creator.create_segmented_video_op(graph)
        visualization_ops.append(segmented_video_op)
    return visualization_ops


def create_planning_op(graph):
    planning_op = graph.add(
        ChallengePlanningOperator,
        name='planning',
        init_args={
            'flags': FLAGS,
            'log_file_name': FLAGS.log_file_name,
            'csv_file_name': FLAGS.csv_log_file_name
        })
    return planning_op


def create_agent_op(graph):
    agent_op = graph.add(
        LidarERDOSAgentOperator,
        name='lidar_erdos_agent',
        init_args={
            'flags': FLAGS,
            'log_file_name': FLAGS.log_file_name,
            'csv_file_name': FLAGS.csv_log_file_name
        })
    return agent_op


def create_control_op(graph):
    control_op = graph.add(
        PIDControlOperator,
        name='controller',
        init_args={
            'longitudinal_control_args': {
                'K_P': FLAGS.pid_p,
                'K_I': FLAGS.pid_i,
                'K_D': FLAGS.pid_d,
            },
            'flags': FLAGS,
            'log_file_name': FLAGS.log_file_name,
            'csv_file_name': FLAGS.csv_log_file_name
        })
    return control_op


class ERDOSAgent(AutonomousAgent):

    def setup(self, path_to_conf_file):
        flags.FLAGS([__file__, '--flagfile={}'.format(path_to_conf_file)])
        self.track = Track.ALL_SENSORS_HDMAP_WAYPOINTS
#        self.track = Track.ALL_SENSORS
#        self.track = Track.CAMERAS
        loc = simulation.utils.Location(2.0, 0.0, 1.40)
        self._camera_transform = simulation.utils.Transform(
            loc, pitch=0, yaw=0, roll=0)
        self._lidar_transform = simulation.utils.Transform(
            loc, pitch=0, yaw=0, roll=0)
        self._camera_names = {CENTER_CAMERA_NAME}
        if FLAGS.depth_estimation:
            self._camera_names.add(LEFT_CAMERA_NAME)
            self._camera_names.add(RIGHT_CAMERA_NAME)
        self._camera_streams = {}
        self._lock = threading.Lock()
        self._vehicle_transform = None
        self._waypoints = None
        self._sent_open_drive_data = False
        self._open_drive_data = None
        self._message_num = 0

        # Set up graph
        self.graph = erdos.graph.get_current_graph()

        scenario_input_op = self.__create_scenario_input_op()

        visualization_ops = add_visualization_operators(
            self.graph, CENTER_CAMERA_NAME)

        if FLAGS.depth_estimation:
            left_ops = add_visualization_operators(
                self.graph, LEFT_CAMERA_NAME)
            right_ops = add_visualization_operators(
                self.graph, RIGHT_CAMERA_NAME)
            self.graph.connect([scenario_input_op], left_ops + right_ops)
            depth_estimation_op = operator_creator.create_depth_estimation_op(
                self.graph, LEFT_CAMERA_NAME, RIGHT_CAMERA_NAME)
            self.graph.connect([scenario_input_op], [depth_estimation_op])

        segmentation_ops = []
        if FLAGS.segmentation_drn:
            segmentation_op = operator_creator.create_segmentation_drn_op(
                self.graph)
            segmentation_ops.append(segmentation_op)

        if FLAGS.segmentation_dla:
            segmentation_op = operator_creator.create_segmentation_dla_op(
                self.graph)
            segmentation_ops.append(segmentation_op)

        obj_detector_ops = []
        tracker_ops = []
        if FLAGS.obj_detection:
            obj_detector_ops = operator_creator.create_detector_ops(self.graph)
            if FLAGS.obj_tracking:
                tracker_op = operator_creator.create_object_tracking_op(
                    self.graph)
                tracker_ops.append(tracker_op)

        traffic_light_det_ops = []
        if FLAGS.traffic_light_det:
            traffic_light_det_ops.append(
                operator_creator.create_traffic_light_op(self.graph))

        lane_detection_ops = []
        if FLAGS.lane_detection:
            lane_detection_ops.append(
                operator_creator.create_lane_detection_op(self.graph))

        planning_ops = [create_planning_op(self.graph)]

#        control_op = create_control_op(self.graph)

        agent_op = create_agent_op(self.graph)

        self.graph.connect(
            [scenario_input_op],
            segmentation_ops + obj_detector_ops + tracker_ops +
            traffic_light_det_ops + lane_detection_ops + planning_ops +
            visualization_ops + [agent_op])

        self.graph.connect(segmentation_ops + obj_detector_ops + tracker_ops +
                           traffic_light_det_ops + lane_detection_ops +
                           planning_ops, [agent_op])

        # Execute graph
        self.graph.execute(FLAGS.framework, blocking=False)

        rospy.init_node("erdos_driver", anonymous=True)
        # Subscribe to the control stream
        rospy.Subscriber('default/lidar_erdos_agent/control_stream',
                         String,
                         callback=self.on_control_msg,
                         queue_size=None)

        for name, stream in self._camera_streams.items():
            stream.setup()
        self._global_trajectory_stream.setup()
        self._open_drive_stream.setup()
        self._can_bus_stream.setup()
        if FLAGS.lidar:
            self._point_cloud_stream.setup()

    def sensors(self):
        """
        Define the sensor suite required by the agent.
        """
        # sensors = [{'type': 'sensor.camera.rgb', 'x': 0.7, 'y': -0.4, 'z': 1.60, 'roll': 0.0, 'pitch': 0.0,
        #             'yaw': -45.0, 'width': 800, 'height': 600, 'fov': 100, 'id': 'Left'},
        #            {'type': 'sensor.camera.rgb', 'x': 0.7, 'y': 0.4, 'z': 1.60, 'roll': 0.0, 'pitch': 0.0, 'yaw': 45.0,
        #             'width': 800, 'height': 600, 'fov': 100, 'id': 'Right'},
        #           ]

        can_sensor = [{'type': 'sensor.can_bus',
                       'reading_frequency': 20,
                       'id': 'can_bus'}]
        gps_sensor = [{'type': 'sensor.other.gnss',
                       'x': 0.7,
                       'y': -0.4,
                       'z': 1.60,
                       'id': 'GPS'}]
        hd_map_sensor = [{'type': 'sensor.hd_map',
                          'reading_frequency': 20,
                          'id': 'hdmap'}]

        camera_sensors = [{'type': 'sensor.camera.rgb',
                           'x': self._camera_transform.location.x,
                           'y': self._camera_transform.location.y,
                           'z': self._camera_transform.location.z,
                           'roll': self._camera_transform.rotation.roll,
                           'pitch': self._camera_transform.rotation.pitch,
                           'yaw': self._camera_transform.rotation.yaw,
                           'width': 800,
                           'height': 600,
                           'fov': 100,
                           'id': CENTER_CAMERA_NAME}]
        if FLAGS.depth_estimation:
            left_camera_sensor = {'type': 'sensor.camera.rgb',
                                  'x': 2.0,
                                  'y': -0.4,
                                  'z': 1.40,
                                  'roll': 0,
                                  'pitch': 0,
                                  'yaw': 0,
                                  'width': 800,
                                  'height': 600,
                                  'fov': 100,
                                  'id': LEFT_CAMERA_NAME}
            camera_sensors.append(left_camera_sensor)
            right_camera_sensor = {'type': 'sensor.camera.rgb',
                                   'x': 2.0,
                                   'y': 0.4,
                                   'z': 1.40,
                                   'roll': 0,
                                   'pitch': 0,
                                   'yaw': 0,
                                   'width': 800,
                                   'height': 600,
                                   'fov': 100,
                                   'id': RIGHT_CAMERA_NAME}
            camera_sensors.append(right_camera_sensor)

        lidar_sensor = []
        if FLAGS.lidar:
            lidar_sensor = [{'type': 'sensor.lidar.ray_cast',
                             'x': self._lidar_transform.location.x,
                             'y': self._lidar_transform.location.y,
                             'z': self._lidar_transform.location.z,
                             'roll': self._lidar_transform.rotation.roll,
                             'pitch': self._lidar_transform.rotation.pitch,
                             'yaw': self._lidar_transform.rotation.yaw,
                             'id': 'LIDAR'}]
        return can_sensor + gps_sensor + hd_map_sensor + camera_sensors + lidar_sensor

    def run_step(self, input_data, timestamp):
        with self._lock:
            self._control = None
            self._control_timestamp = None
        game_time = int(timestamp * 1000)
        erdos_timestamp = Timestamp(coordinates=[game_time, self._message_num])
        watermark = WatermarkMessage(erdos_timestamp)
        self._message_num += 1

        # Send once the global waypoints.
        if self._waypoints is None:
            self._waypoints = self._global_plan_world_coord
            data = [(simulation.utils.to_erdos_transform(transform), road_option)
                    for (transform, road_option) in self._waypoints]
            self._global_trajectory_stream.send(Message(data, erdos_timestamp))
            self._global_trajectory_stream.send(watermark)
        else:
            self._global_trajectory_stream.send(watermark)
        assert self._waypoints == self._global_plan_world_coord,\
            'Global plan has been updated.'

        for key, val in input_data.items():
            #print("{} {} {}".format(key, val[0], type(val[1])))
            if key in self._camera_names:
                self._camera_streams[key].send(
                    simulation.messages.FrameMessage(
                        pylot_utils.bgra_to_bgr(val[1]),
                        erdos_timestamp))
                self._camera_streams[key].send(watermark)
            elif key == 'can_bus':
                # The can bus dict contains other fields as well, but we don't
                # curently use them.
                self._vehicle_transform = simulation.utils.to_erdos_transform(
                    val[1]['transform'])
                # TODO(ionel): Scenario runner computes speed differently from
                # the way we do it in the CARLA operator. This affects
                # agent stopping constants. Check!
                forward_speed = val[1]['speed']
                can_bus = simulation.utils.CanBus(
                    self._vehicle_transform, forward_speed)
                self._can_bus_stream.send(Message(can_bus, erdos_timestamp))
                self._can_bus_stream.send(watermark)
            elif key == 'GPS':
                gps = simulation.utils.LocationGeo(
                    val[1][0], val[1][1], val[1][2])
            elif key == 'hdmap':
                # Sending once opendrive data
                if not self._sent_open_drive_data:
                    self._open_drive_data = val[1]['opendrive']
                    self._sent_open_drive_data = True
                    self._open_drive_stream.send(
                        Message(self._open_drive_data, erdos_timestamp))
                    # TODO(ionel): We should have a top watermark.
                    # This is dangerous!
                    top_watermark = WatermarkMessage(
                        Timestamp(coordinates=[1000000000.0, 1000000000]))
                self._open_drive_stream.send(watermark)
                assert self._open_drive_data == val[1]['opendrive'],\
                    'Opendrive data changed.'

                # TODO(ionel): Send point cloud data.
                pc_file = val[1]['map_file']
            elif key == 'LIDAR':
                msg = simulation.messages.PointCloudMessage(
                    point_cloud=val[1],
                    transform=self._lidar_transform,
                    timestamp=erdos_timestamp)
                self._point_cloud_stream.send(msg)
                self._point_cloud_stream.send(watermark)

        # Wait until the control is set.
        while self._control_timestamp is None or self._control_timestamp < erdos_timestamp:
            time.sleep(0.01)

        return self._control

    def on_control_msg(self, msg):
        msg = pickle.loads(msg.data)
        if not isinstance(msg, WatermarkMessage):
            with self._lock:
                print("Received control message {}".format(msg))
                self._control_timestamp = msg.timestamp
                self._control = carla.VehicleControl()
                self._control.throttle = msg.throttle
                self._control.brake = msg.brake
                self._control.steer = msg.steer
                self._control.reverse = msg.reverse
                self._control.hand_brake = msg.hand_brake
                self._control.manual_gear_shift = False

    def __create_scenario_input_op(self):
        for name in self._camera_names:
            stream = ROSOutputDataStream(
                DataStream(name=name,
                           uid=name,
                           labels={'sensor_type': 'camera',
                                   'camera_type': 'sensor.camera.rgb'}))
            self._camera_streams[name] = stream

        # Stream on which we send the global trajectory.
        self._global_trajectory_stream = ROSOutputDataStream(
            DataStream(name='global_trajectory_stream',
                       uid='global_trajectory_stream',
                       labels={'global': 'true',
                               'waypoints': 'true'}))

        # Stream on which we send the opendrive map.
        self._open_drive_stream = ROSOutputDataStream(
            DataStream(name='open_drive_stream',
                       uid='open_drive_stream'))

        self._can_bus_stream = ROSOutputDataStream(
            DataStream(name='can_bus', uid='can_bus'))

        input_streams = (self._camera_streams.values() +
                         [self._global_trajectory_stream,
                          self._open_drive_stream,
                          self._can_bus_stream])

        if FLAGS.lidar:
            self._point_cloud_stream = ROSOutputDataStream(
                DataStream(name='lidar',
                           uid='lidar',
                           labels={'sensor_type': 'sensor.lidar.ray_cast'}))
            input_streams.append(self._point_cloud_stream)

        return self.graph.add(NoopOp,
                              name='scenario_input',
                              input_streams=input_streams)
