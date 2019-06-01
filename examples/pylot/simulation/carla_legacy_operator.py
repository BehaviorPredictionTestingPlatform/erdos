import numpy as np
import time
import ray

from carla.client import CarlaClient
from carla.sensor import Camera, Lidar
from carla.settings import CarlaSettings

from erdos.data_stream import DataStream
from erdos.message import Message, WatermarkMessage
from erdos.op import Op
from erdos.timestamp import Timestamp
from erdos.utils import frequency, setup_csv_logging, setup_logging, time_epoch_ms

from perception.messages import SegmentedFrameMessage
import pylot_utils
import simulation.messages
from simulation.utils import depth_to_array, labels_to_array, to_bgra_array
import simulation.utils


class CarlaLegacyOperator(Op):
    """Provides an ERDOS interface to the CARLA simulator.

    Args:
        synchronous_mode (bool): whether the simulator will wait for control
            input from the client.
    """
    def __init__(self,
                 name,
                 flags,
                 camera_setups=[],
                 lidar_setups=[],
                 log_file_name=None,
                 csv_file_name=None):
        super(CarlaLegacyOperator, self).__init__(name)
        self._flags = flags
        self._logger = setup_logging(self.name, log_file_name)
        self._csv_logger = setup_csv_logging(self.name + '-csv', csv_file_name)
        self.message_num = 0
        if self._flags.carla_high_quality:
            quality = 'Epic'
        else:
            quality = 'Low'
        self.settings = CarlaSettings()
        self.settings.set(
            SynchronousMode=self._flags.carla_synchronous_mode,
            SendNonPlayerAgentsInfo=True,
            NumberOfVehicles=self._flags.carla_num_vehicles,
            NumberOfPedestrians=self._flags.carla_num_pedestrians,
            WeatherId=self._flags.carla_weather,
            QualityLevel=quality)
        self.settings.randomize_seeds()
        self._transforms = {}
        for cs in camera_setups:
            transform = self.__add_camera(name=cs.name,
                                          postprocessing=cs.type,
                                          image_size=cs.resolution,
                                          position=cs.pos)
            self._transforms[cs.name] = transform
        for ls in lidar_setups:
            self.__add_lidar(name=ls.name,
                             channels=ls.channels,
                             range=ls.range,
                             points_per_second=ls.points_per_second,
                             rotation_frequency=ls.rotation_frequency,
                             upper_fov=ls.upper_fov,
                             lower_fov=ls.lower_fov,
                             position=ls.pos)
        self.agent_id_map = {}
        self.pedestrian_count = 0
        # Register custom serializers for Messages and WatermarkMessages
        ray.register_custom_serializer(Message, use_pickle=True)
        ray.register_custom_serializer(WatermarkMessage, use_pickle=True)

    @staticmethod
    def setup_streams(input_streams, camera_setups, lidar_setups):
        input_streams.add_callback(CarlaLegacyOperator.update_control)
        camera_streams = [pylot_utils.create_camera_stream(cs)
                          for cs in camera_setups]
        lidar_streams = [pylot_utils.create_lidar_stream(ls)
                         for ls in lidar_setups]
        return [
            DataStream(name='can_bus'),
            DataStream(name='traffic_lights'),
            DataStream(name='pedestrians'),
            DataStream(name='vehicles'),
            DataStream(name='traffic_signs'),
        ] + camera_streams + lidar_streams

    def __add_camera(self,
                     name,
                     postprocessing,
                     image_size=(800, 600),
                     field_of_view=90.0,
                     position=(0.3, 0, 1.3),
                     rotation_pitch=0,
                     rotation_roll=0,
                     rotation_yaw=0):
        """Adds a camera and a corresponding output stream.

        Args:
            name: A string naming the camera.
            postprocessing: "SceneFinal", "Depth", "SemanticSegmentation".
        """
        if postprocessing == 'sensor.camera.rgb':
            postprocessing = 'SceneFinal'
        elif postprocessing == 'sensor.camera.depth':
            postprocessing = 'Depth'
        elif postprocessing == 'sensor.camera.semantic_segmentation':
            postprocessing = 'SemanticSegmentation'

        camera = Camera(
            name,
            PostProcessing=postprocessing,
            FOV=field_of_view,
            ImageSizeX=image_size[0],
            ImageSizeY=image_size[1],
            PositionX=position[0],
            PositionY=position[1],
            PositionZ=position[2],
            RotationPitch=rotation_pitch,
            RotationRoll=rotation_roll,
            RotationYaw=rotation_yaw)

        self.settings.add_sensor(camera)
        return camera.get_transform()

    def __add_lidar(self,
                    name,
                    channels=32,
                    range=50,
                    points_per_second=100000,
                    rotation_frequency=10,
                    upper_fov=10,
                    lower_fov=-30,
                    position=(0, 0, 1.4),
                    rotation_pitch=0,
                    rotation_yaw=0,
                    rotation_roll=0):
        """Adds a LIDAR sensor and a corresponding output stream.

        Args:
            name: A string naming the camera.
        """
        lidar = Lidar(
            name,
            Channels=channels,
            Range=range,
            PointsPerSecond=points_per_second,
            RotationFrequency=rotation_frequency,
            UpperFovLimit=upper_fov,
            LowerFovLimit=lower_fov,
            PositionX=position[0],
            PositionY=position[1],
            PositionZ=position[2],
            RotationPitch=rotation_pitch,
            RotationYaw=rotation_yaw,
            RotationRoll=rotation_roll)

        self.settings.add_sensor(lidar)

    def read_carla_data(self):
        read_start_time = time.time()
        measurements, sensor_data = self.client.read_data()
        measure_time = time.time()

        self._logger.info(
            'Got readings for game time {} and platform time {}'.format(
                measurements.game_timestamp, measurements.platform_timestamp))

        timestamp = Timestamp(
            coordinates=[measurements.game_timestamp, self.message_num])
        self.message_num += 1
        watermark = WatermarkMessage(timestamp)

        # Send player data on data streams.
        self.__send_player_data(measurements.player_measurements, timestamp, watermark)
        # Extract agent data from measurements.
        agents = self.__get_ground_agents(measurements)
        # Send agent data on data streams.
        self.__send_ground_agent_data(agents, timestamp, watermark)
        # Send sensor data on data stream.
        self.__send_sensor_data(sensor_data, timestamp, watermark)
        # Send control command to the simulator.
        self.client.send_control(**self.control)
        end_time = time.time()

        measurement_runtime = (measure_time - read_start_time) * 1000
        total_runtime = (end_time - read_start_time) * 1000
        self._logger.info('Carla measurement time {}; total time {}'.format(
            measurement_runtime, total_runtime))
        self._csv_logger.info('{},{},{},{}'.format(
            time_epoch_ms(), self.name, measurement_runtime, total_runtime))

    def __send_player_data(self, player_measurements, timestamp, watermark):
        location = simulation.utils.Location(
            carla_loc=player_measurements.transform.location)
        orientation = simulation.utils.Orientation(
            player_measurements.transform.orientation.x,
            player_measurements.transform.orientation.y,
            player_measurements.transform.orientation.z)
        vehicle_transform = simulation.utils.Transform(
            location,
            player_measurements.transform.rotation.pitch,
            player_measurements.transform.rotation.yaw,
            player_measurements.transform.rotation.roll,
            orientation=orientation)
        forward_speed = player_measurements.forward_speed * 3.6
        can_bus = simulation.utils.CanBus(vehicle_transform, forward_speed)
        self.get_output_stream('can_bus').send(Message(can_bus, timestamp))
        self.get_output_stream('can_bus').send(watermark)

    def __get_ground_agents(self, measurements):
        vehicles = []
        pedestrians = []
        traffic_lights = []
        speed_limit_signs = []
        for agent in measurements.non_player_agents:
            if agent.HasField('vehicle'):
                pos = simulation.utils.Location(
                    carla_loc=agent.vehicle.transform.location)
                transform = simulation.utils.to_erdos_transform(
                    agent.vehicle.transform)
                bb = simulation.utils.BoundingBox(agent.vehicle.bounding_box)
                forward_speed = agent.vehicle.forward_speed
                vehicle = simulation.utils.Vehicle(pos, transform, bb, forward_speed)
                vehicles.append(vehicle)
            elif agent.HasField('pedestrian'):
                if not self.agent_id_map.get(agent.id):
                    self.pedestrian_count += 1
                    self.agent_id_map[agent.id] = self.pedestrian_count

                pedestrian_index = self.agent_id_map[agent.id]
                pos = simulation.utils.Location(
                    carla_loc=agent.pedestrian.transform.location)
                transform = simulation.utils.to_erdos_transform(
                    agent.pedestrian.transform)
                bb = simulation.utils.BoundingBox(agent.pedestrian.bounding_box)
                forward_speed = agent.pedestrian.forward_speed
                pedestrian = simulation.utils.Pedestrian(
                    pedestrian_index, pos, transform, bb, forward_speed)
                pedestrians.append(pedestrian)
            elif agent.HasField('traffic_light'):
                pos = simulation.utils.Location(
                    carla_loc=agent.traffic_light.transform.location)
                transform = simulation.utils.to_erdos_transform(
                    agent.traffic_light.transform)
                traffic_light = simulation.utils.TrafficLight(
                    pos, transform, agent.traffic_light.state)
                traffic_lights.append(traffic_light)
            elif agent.HasField('speed_limit_sign'):
                pos = simulation.utils.Location(
                    carla_loc=agent.speed_limit_sign.transform.location)
                transform = simulation.utils.to_erdos_transform(
                    agent.speed_limit_sign.transform)
                speed_sign = simulation.utils.SpeedLimitSign(
                    pos, transform, agent.speed_limit_sign.speed_limit)
                speed_limit_signs.append(speed_sign)

        return vehicles, pedestrians, traffic_lights, speed_limit_signs

    def __send_ground_agent_data(self, agents, timestamp, watermark):
        vehicles, pedestrians, traffic_lights, speed_limit_signs = agents
        vehicles_msg = simulation.messages.GroundVehiclesMessage(
            vehicles, timestamp)
        self.get_output_stream('vehicles').send(vehicles_msg)
        self.get_output_stream('vehicles').send(watermark)
        pedestrians_msg = simulation.messages.GroundPedestriansMessage(
            pedestrians, timestamp)
        self.get_output_stream('pedestrians').send(pedestrians_msg)
        self.get_output_stream('pedestrians').send(watermark)
        traffic_lights_msg = simulation.messages.GroundTrafficLightsMessage(
            traffic_lights, timestamp)
        self.get_output_stream('traffic_lights').send(traffic_lights_msg)
        self.get_output_stream('traffic_lights').send(watermark)
        traffic_sings_msg = simulation.messages.GroundSpeedSignsMessage(
            speed_limit_signs, timestamp)
        self.get_output_stream('traffic_signs').send(traffic_sings_msg)
        self.get_output_stream('traffic_signs').send(watermark)

    def __send_sensor_data(self, sensor_data, timestamp, watermark):
        for name, measurement in sensor_data.items():
            data_stream = self.get_output_stream(name)
            if data_stream.get_label('camera_type') == 'sensor.camera.rgb':
                # Transform the Carla RGB images to BGR.
                data_stream.send(
                    simulation.messages.FrameMessage(
                        pylot_utils.bgra_to_bgr(to_bgra_array(measurement)), timestamp))
            elif data_stream.get_label('camera_type') == 'sensor.camera.semantic_segmentation':
                frame = labels_to_array(measurement)
                data_stream.send(SegmentedFrameMessage(frame, 0, timestamp))
            elif data_stream.get_label('camera_type') == 'sensor.camera.depth':
                # NOTE: depth_to_array flips the image.
                data_stream.send(
                    simulation.messages.DepthFrameMessage(
                        depth_to_array(measurement),
                        self._transforms[name],
                        measurement.fov,
                        timestamp))
            else:
                data_stream.send(Message(measurement, timestamp))
            data_stream.send(watermark)

    def read_data_at_frequency(self):
        period = 1.0 / self._flags.carla_step_frequency
        trigger_at = time.time() + period
        while True:
            time_until_trigger = trigger_at - time.time()
            if time_until_trigger > 0:
                time.sleep(time_until_trigger)
            else:
                self._logger.error('Cannot read Carla data at frequency {}'.format(
                    self._flags.carla_step_frequency))
            self.read_carla_data()
            trigger_at += period

    def update_control(self, msg):
        """Updates the control dict"""
        self.control['steer'] = msg.steer
        self.control['throttle'] = msg.throttle
        self.control['brake'] = msg.brake
        self.control['hand_brake'] = msg.hand_brake
        self.control['reverse'] = msg.reverse

    def execute(self):
        # Subscribe to control streams
        self.control = {
            'steer': 0.0,
            'throttle': 0.0,
            'brake': 0.0,
            'hand_brake': False,
            'reverse': False
        }
        self.client = CarlaClient(self._flags.carla_host,
                                  self._flags.carla_port,
                                  timeout=10)
        self.client.connect()
        scene = self.client.load_settings(self.settings)

        # Choose one player start at random.
        number_of_player_starts = len(scene.player_start_spots)
        player_start = self._flags.carla_start_player_num
        if self._flags.carla_random_player_start:
            player_start = np.random.randint(
                0, max(0, number_of_player_starts - 1))

        self.client.start_episode(player_start)

        self.read_data_at_frequency()
        self.spin()
