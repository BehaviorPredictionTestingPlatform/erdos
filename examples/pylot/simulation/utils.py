from collections import namedtuple
from itertools import combinations
import math
import numpy as np
from numpy.linalg import inv
from numpy.matlib import repmat

from simulation.messages import Location, Rotation

Extent = namedtuple('Extent', 'x, y, z')
Scale = namedtuple('Scale', 'x y z')
Scale.__new__.__defaults__ = (1.0, 1.0, 1.0)


class BoundingBox(object):
    def __init__(self, bb):
        pos = Location(bb.transform.location.x,
                       bb.transform.location.y,
                       bb.transform.location.z)
        self.transform = Transform(pos,
                                   bb.transform.rotation.pitch,
                                   bb.transform.rotation.yaw,
                                   bb.transform.rotation.roll)
        self.extent = Extent(bb.extent.x, bb.extent.y, bb.extent.z)

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        return "transform: {}, x: {}, y: {}, z: {}".format(str(self.transform), self.extent)

class Transform(object):

    def __init__(self, pos=None, pitch=0, yaw=0, roll=0, orientation=None,
                 scale=None, matrix=None):
        self.orientation = orientation
        self.rotation = Rotation(pitch, yaw, roll)
        self.location = pos
        if scale is None:
            scale = Scale()
        if matrix is None:
            self.matrix = np.matrix(np.identity(4))
            cy = math.cos(np.radians(yaw))
            sy = math.sin(np.radians(yaw))
            cr = math.cos(np.radians(roll))
            sr = math.sin(np.radians(roll))
            cp = math.cos(np.radians(pitch))
            sp = math.sin(np.radians(pitch))
            self.matrix[0, 3] = pos.x
            self.matrix[1, 3] = pos.y
            self.matrix[2, 3] = pos.z
            self.matrix[0, 0] = scale.x * (cp * cy)
            self.matrix[0, 1] = scale.y * (cy * sp * sr - sy * cr)
            self.matrix[0, 2] = -scale.z * (cy * sp * cr + sy * sr)
            self.matrix[1, 0] = scale.x * (sy * cp)
            self.matrix[1, 1] = scale.y * (sy * sp * sr + cy * cr)
            self.matrix[1, 2] = scale.z * (cy * sr - sy * sp * cr)
            self.matrix[2, 0] = scale.x * (sp)
            self.matrix[2, 1] = -scale.y * (cp * sr)
            self.matrix[2, 2] = scale.z * (cp * cr)
        else:
            self.matrix = matrix

    def transform_points(self, points):
        """
        Given a 4x4 transformation matrix, transform an array of 3D points.
        Expected point foramt: [[X0,Y0,Z0],..[Xn,Yn,Zn]]
        """
        # Needed format: [[X0,..Xn],[Z0,..Zn],[Z0,..Zn]]. So let's transpose
        # the point matrix.
        points = points.transpose()
        # Add 0s row: [[X0..,Xn],[Y0..,Yn],[Z0..,Zn],[0,..0]]
        points = np.append(points, np.ones((1, points.shape[1])), axis=0)
        # Point transformation
        points = self.matrix * points
        # Return all but last row
        return points[0:3].transpose()

    def __mul__(self, other):
        return Transform(matrix=np.dot(self.matrix, other.matrix))

    def __str__(self):
        return str(self.matrix)


def depth_to_local_point_cloud(depth_msg, max_depth=0.9):
    far = 1000.0  # max depth in meters.
    normalized_depth = depth_msg.frame
    # (Intrinsic) K Matrix
    k = np.identity(3)
    k[0, 2] = depth_msg.width / 2.0
    k[1, 2] = depth_msg.height / 2.0
    k[0, 0] = k[1, 1] = depth_msg.width / \
        (2.0 * math.tan(depth_msg.fov * math.pi / 360.0))
    # 2d pixel coordinates
    pixel_length = depth_msg.width * depth_msg.height
    u_coord = repmat(np.r_[depth_msg.width-1:-1:-1],
                     depth_msg.height, 1).reshape(pixel_length)
    v_coord = repmat(np.c_[depth_msg.height-1:-1:-1],
                     1, depth_msg.width).reshape(pixel_length)
    normalized_depth = np.reshape(normalized_depth, pixel_length)

    # Search for pixels where the depth is greater than max_depth to
    # delete them
    max_depth_indexes = np.where(normalized_depth > max_depth)
    normalized_depth = np.delete(normalized_depth, max_depth_indexes)
    u_coord = np.delete(u_coord, max_depth_indexes)
    v_coord = np.delete(v_coord, max_depth_indexes)

    # pd2 = [u,v,1]
    p2d = np.array([u_coord, v_coord, np.ones_like(u_coord)])

    # P = [X,Y,Z]
    p3d = np.dot(np.linalg.inv(k), p2d)
    p3d *= normalized_depth * far

    # [[X1,Y1,Z1],[X2,Y2,Z2], ... [Xn,Yn,Zn]]
    return np.transpose(p3d)


def get_3d_world_position(x, y, depth_msg, vehicle_transform):
    far = 1.0
    point_cloud = depth_to_local_point_cloud(depth_msg, max_depth=far)
    car_transform = vehicle_transform * depth_msg.transform
    point_cloud = car_transform.transform_points(point_cloud)
    (x, y, z) = point_cloud.tolist()[y * depth_msg.width + x]
    return Location(x, y, z)


def get_camera_intrinsic_and_transform(image_size=(800, 600),
                                       position=(2.0, 0.0, 1.4),
                                       rotation_pitch=0,
                                       rotation_roll=0,
                                       rotation_yaw=0):

    image_width = image_size[0]
    image_height = image_size[1]
    # (Intrinsic) K Matrix
    intrinsic_mat = np.identity(3)
    intrinsic_mat[0][2] = image_width / 2
    intrinsic_mat[1][2] = image_height / 2
    intrinsic_mat[0][0] = intrinsic_mat[1][1] = image_width / (2.0 * math.tan(90.0 * math.pi / 360.0))

    pos = Location(position[0], position[1], position[2])
    transform = Transform(pos, rotation_pitch, rotation_roll, rotation_yaw)
    to_unreal_transform = Transform(Location(0, 0, 0), 0, -90, -90, Scale(x=-1))
    camera_transform = transform * to_unreal_transform

    return (intrinsic_mat, camera_transform, (image_width, image_height))


def get_bounding_box_from_corners(corners):
    """
    Gets the bounding box of the pedestrian given the corners of the plane.
    """
    # Figure out the opposite ends of the rectangle. Our 2D mapping doesn't
    # return perfect rectangular coordinates and also doesn't return them
    # in clockwise order.
    max_distance = 0
    opp_ends = None
    for (a, b) in combinations(corners, r=2):
        if abs(a[0] - b[0]) <= 0.8 or abs(a[1] - b[1]) <= 0.8:
            # The points are too close. They may be lying on the same axis.
            # Move forward.
            pass
        else:
            # The points possibly lie on different axis. Choose the two
            # points which are the farthest.
            distance = (b[0] - a[0])**2 + (b[1] - a[1])**2
            if distance > max_distance:
                max_distance = distance
                if a[0] < b[0] and a[1] < b[1]:
                    opp_ends = (a, b)
                else:
                    opp_ends = (b, a)

    # If we were able to find two points far enough to be considered as
    # possible bounding boxes, return the results.
    return opp_ends


def get_bounding_box_sampling_points(ends):
    """
    Get the sampling points given the ends of the rectangle.
    """
    a, b = ends

    # Find the middle point of the rectangle, and see if the points
    # around it are visible from the camera.
    middle_point = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2,
                    b[2].flatten().item(0))
    sampling_points = [
        middle_point,
        (middle_point[0] + 2, middle_point[1], middle_point[2]),
        (middle_point[0] + 1, middle_point[1] + 1, middle_point[2]),
        (middle_point[0] + 1, middle_point[1] - 1, middle_point[2]),
        (middle_point[0] - 2, middle_point[1], middle_point[2]),
        (middle_point[0] - 1, middle_point[1] + 1, middle_point[2]),
        (middle_point[0] - 1, middle_point[1] - 1, middle_point[2])
    ]
    return (middle_point, sampling_points)


def get_2d_bbox_from_3d_box(
        depth_array, vehicle_transform, obj_transform,
        bounding_box, rgb_transform, rgb_intrinsic, rgb_img_size,
        middle_depth_threshold, neighbor_threshold):
    corners = map_ground_bounding_box_to_2D(
        vehicle_transform, obj_transform,
        bounding_box, rgb_transform, rgb_intrinsic,
        rgb_img_size)
    if len(corners) == 8:
        ends = get_bounding_box_from_corners(corners)
        if ends:
            (middle_point, points) = get_bounding_box_sampling_points(ends)
            # Select bounding box if the middle point in inside the frame
            # and has the same depth
            if (inside_image(middle_point[0], middle_point[1],
                             rgb_img_size[0], rgb_img_size[1]) and
                have_same_depth(middle_point[0],
                                middle_point[1],
                                middle_point[2],
                                depth_array,
                                middle_depth_threshold)):
                (xmin, xmax, ymin, ymax) = select_max_bbox(ends)
                width = xmax - xmin
                height = ymax - ymin
                # Filter out the small bounding boxes (they're far away).
                # We use thresholds that are proportional to the image size.
                # XXX(ionel): Reduce thresholds to 0.01, 0.01, and 0.0002 if
                # you want to include objects that are far away.
                if (width > rgb_img_size[0] * 0.01 and
                    height > rgb_img_size[1] * 0.02 and
                    width * height > rgb_img_size[0] * rgb_img_size[1] * 0.0004):
                    return (xmin, xmax, ymin, ymax)
            else:
                # The mid point doesn't have the same depth. It can happen
                # for valid boxes when the mid point is between the legs.
                # In this case, we check that a fraction of the neighbouring
                # points have the same depth.
                # Filter the points inside the image.
                points_inside_image = [
                    (x, y, z)
                    for (x, y, z) in points if inside_image(
                            x, y, rgb_img_size[0], rgb_img_size[1])
                ]
                same_depth_points = [
                    have_same_depth(x, y, z, depth_array, neighbor_threshold)
                            for (x, y, z) in points_inside_image
                ]
                if len(same_depth_points) > 0 and \
                        same_depth_points.count(True) >= 0.4 * len(same_depth_points):
                    (xmin, xmax, ymin, ymax) = select_max_bbox(ends)
                    width = xmax - xmin
                    height = ymax - ymin
                    width = xmax - xmin
                    height = ymax - ymin
                    # Filter out the small bounding boxes (they're far away).
                    # We use thresholds that are proportional to the image size.
                    # XXX(ionel): Reduce thresholds to 0.01, 0.01, and 0.0002 if
                    # you want to include objects that are far away.
                    if (width > rgb_img_size[0] * 0.01 and
                        height > rgb_img_size[1] * 0.02 and
                        width * height > rgb_img_size[0] * rgb_img_size[1] * 0.0004):
                        return (xmin, xmax, ymin, ymax)


def have_same_depth(x, y, z, depth_array, threshold):
    x, y = int(x), int(y)
    return abs(depth_array[y][x] * 1000 - z) < threshold


def inside_image(x, y, img_width, img_height):
    return x >= 0 and y >= 0 and x < img_width and y < img_height


def select_max_bbox(ends):
    (xmin, ymin) = tuple(map(int, ends[0][:2]))
    (xmax, ymax) = tuple(map(int, ends[0][:2]))
    corner = tuple(map(int, ends[1][:2]))
    # XXX(ionel): This is not quite correct. We get the
    # minimum and maximum x and y values, but these may
    # not be valid points. However, it works because the
    # bboxes are parallel to x and y axis.
    xmin = min(xmin, corner[0])
    ymin = min(ymin, corner[1])
    xmax = max(xmax, corner[0])
    ymax = max(ymax, corner[1])
    return (xmin, xmax, ymin, ymax)


def map_ground_bounding_box_to_2D(vehicle_transform,
                                  obj_transform,
                                  bounding_box,
                                  rgb_transform,
                                  rgb_intrinsic,
                                  rgb_img_size):
    (image_width, image_height) = rgb_img_size
    extrinsic_mat = vehicle_transform * rgb_transform

    # 8 bounding box vertices relative to (0,0,0)
    bbox = np.array([
        [  bounding_box.extent.x,   bounding_box.extent.y,   bounding_box.extent.z],
        [  bounding_box.extent.x, - bounding_box.extent.y,   bounding_box.extent.z],
        [  bounding_box.extent.x,   bounding_box.extent.y, - bounding_box.extent.z],
        [  bounding_box.extent.x, - bounding_box.extent.y, - bounding_box.extent.z],
        [- bounding_box.extent.x,   bounding_box.extent.y,   bounding_box.extent.z],
        [- bounding_box.extent.x, - bounding_box.extent.y,   bounding_box.extent.z],
        [- bounding_box.extent.x,   bounding_box.extent.y, - bounding_box.extent.z],
        [- bounding_box.extent.x, - bounding_box.extent.y, - bounding_box.extent.z]
    ])

    # Transform the vertices with respect to the bounding box transform.
    bbox = bounding_box.transform.transform_points(bbox)

    # The bounding box transform is with respect to the object transform.
    # Transform the points relative to its transform.
    bbox = obj_transform.transform_points(bbox)

    # Object's transform is relative to the world. Thus, the bbox contains
    # the 3D bounding box vertices relative to the world.

    coords = []
    for vertex in bbox:
        pos_vector = np.array([
            [vertex[0,0]],  # [[X,
            [vertex[0,1]],  #   Y,
            [vertex[0,2]],  #   Z,
            [1.0]           #   1.0]]
        ])
        # Transform the points to camera.
        transformed_3d_pos = np.dot(inv(extrinsic_mat.matrix), pos_vector)
        # Transform the points to 2D.
        pos2d = np.dot(rgb_intrinsic, transformed_3d_pos[:3])

        # Normalize the 2D points.
        loc_2d = Location(float(image_width - pos2d[0] / pos2d[2]),
                          float(image_height - pos2d[1] / pos2d[2]),
                          pos2d[2])
        # Add the points to the image.
        if loc_2d.z > 0: # If the point is in front of the camera.
            if (loc_2d.x >= 0 or loc_2d.y >= 0) and (loc_2d.x < image_width or loc_2d.y < image_height):
                coords.append((loc_2d.x, loc_2d.y, loc_2d.z))

    return coords


def map_ground_3D_transform_to_2D(location,
                                  vehicle_transform,
                                  rgb_transform,
                                  rgb_intrinsic,
                                  rgb_img_size):
    extrinsic_mat = vehicle_transform * rgb_transform
    pos_vector = np.array([[location.x], [location.y], [location.z], [1.0]])
    transformed_3d_pos = np.dot(inv(extrinsic_mat.matrix), pos_vector)
    pos2d = np.dot(rgb_intrinsic, transformed_3d_pos[:3])
    (img_width, img_height) = rgb_img_size
    loc_2d = Location(img_width - pos2d[0] / pos2d[2],
                      img_height- pos2d[1] / pos2d[2],
                      pos2d[2])
    if (loc_2d.z > 0 and loc_2d.x >= 0 and loc_2d.x < img_width and
        loc_2d.y >= 0 and loc_2d.y < img_height):
        return (loc_2d.x, loc_2d.y, loc_2d.z)
    return None
