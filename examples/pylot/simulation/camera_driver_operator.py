import threading

import carla

from perception.messages import SegmentedFrameMessage
import pylot_utils
import simulation.carla_utils
import simulation.utils
from simulation.utils import depth_to_array, labels_to_array, to_bgra_array

# ERDOS specific imports.
from erdos.op import Op
from erdos.utils import setup_logging
from erdos.message import WatermarkMessage
from erdos.timestamp import Timestamp


class CameraDriverOperator(Op):
    """ CameraDriverOperator publishes images onto the desired stream from a camera.

    This operator attaches a vehicle at the required position with respect to
    the vehicle, registers callback functions to retrieve the images and
    publishes it to downstream operators.

    Attributes:
        _camera_setup: A CameraSetup tuple.
        _camera: Handle to the camera inside the simulation.
    """
    def __init__(self,
                 name,
                 camera_setup,
                 flags,
                 log_file_name=None):
        """ Initializes the camera inside the simulation with the given
        parameters.

        Args:
            name: The unique name of the operator.
            camera_setup: A CameraSetup tuple.
            flags: A handle to the global flags instance to retrieve the
                configuration.
            log_file_name: The file to log the required information to.
        """
        super(CameraDriverOperator, self).__init__(name)
        self._flags = flags
        self._logger = setup_logging(self.name, log_file_name)
        self._camera_setup = camera_setup

        _, self._world = simulation.carla_utils.get_world(
            self._flags.carla_host,
            self._flags.carla_port)
        if self._world is None:
            raise ValueError("There was an issue connecting to the simulator.")

        # Starts from 1 because the world ticks once before the drivers are
        # added.
        self._message_cnt = 1
        self._vehicle = None
        self._camera = None
        self._lock = threading.Lock()

    @staticmethod
    def setup_streams(input_streams, camera_setup):
        """ Set up callback functions on the input streams and return the
        output stream that publishes the images.

        Args:
            input_streams: The streams that this operator is connected to.
            camera_setup: A CameraSetup tuple.
        """
        input_streams.filter(pylot_utils.is_ground_vehicle_id_stream)\
                     .add_callback(CameraDriverOperator.on_vehicle_id)
        return [pylot_utils.create_camera_stream(camera_setup)]

    def process_images(self, carla_image):
        with self._lock:
            game_time = int(carla_image.timestamp * 1000)
            timestamp = Timestamp(coordinates=[game_time, self._message_cnt])
            watermark_msg = WatermarkMessage(timestamp)
            self._message_cnt += 1

            msg = None
            if self._camera_setup.type == 'sensor.camera.rgb':
                msg = simulation.messages.FrameMessage(
                    pylot_utils.bgra_to_bgr(to_bgra_array(carla_image)), timestamp)
            elif self._camera_setup.type == 'sensor.camera.depth':
                msg = simulation.messages.DepthFrameMessage(
                    depth_to_array(carla_image),
                    simulation.utils.to_erdos_transform(carla_image.transform),
                    carla_image.fov,
                    timestamp)
            elif self._camera_setup.type == 'sensor.camera.semantic_segmentation':
                frame = labels_to_array(carla_image)
                msg = SegmentedFrameMessage(frame, 0, timestamp)
                # Send the message containing the frame.
            self.get_output_stream(self._camera_setup.name).send(msg)
            self.get_output_stream(self._camera_setup.name).send(watermark_msg)

    def on_vehicle_id(self, msg):
        """ This function receives the identifier for the vehicle, retrieves
        the handler for the vehicle from the simulation and attaches the
        camera to it.

        Args:
            msg: The identifier for the vehicle to attach the camera to.
        """
        vehicle_id = msg.data
        self._logger.info(
            "The CameraDriverOperator received the vehicle identifier: {}".format(
                vehicle_id))

        self._vehicle = self._world.get_actors().find(vehicle_id)
        if self._vehicle is None:
            raise ValueError("There was an issue finding the vehicle.")

        # Install the camera.
        camera_blueprint = self._world.get_blueprint_library().find(
                self._camera_setup.type)

        camera_blueprint.set_attribute('image_size_x',
                                       str(self._camera_setup.resolution[0]))
        camera_blueprint.set_attribute('image_size_y',
                                       str(self._camera_setup.resolution[1]))

        transform = carla.Transform(
            carla.Location(*self._camera_setup.pos),
            carla.Rotation(pitch=0, yaw=0, roll=0),
        )

        self._logger.info("Spawning a camera: {}".format(self._camera_setup))

        self._camera = self._world.spawn_actor(camera_blueprint,
                                               transform,
                                               attach_to=self._vehicle)

        # Register the callback on the camera.
        self._camera.listen(self.process_images)
