import os
import time
import timeit
from functools import partial

import pycolmap
import numpy as np
from pycolmap import AbsolutePoseRefinementOptions, RANSACOptions

from src.benchmark.benchmark import SinglePoseBenchmark, SinglePoseBenchmarkResults
from src.config import DATASETS_PATH
from src.dataset.camera import Camera
from src.dataset.camera_pose.enums_and_types import CoordinateSystem, TransformationDirection
from src.dataset.dataset import Dataset
from src.dataset.loaders.colmap_dataset_loader.loader import load_colmap_dataset
from src.dataset.camera_pose.camera_pose import CameraPose


class ColmapSinglePoseBenchmark(SinglePoseBenchmark):
    FRAMEWORK = "Colmap"

    def _prepare_dataset(self):
        mapping = {}
        for index, e in enumerate(self.dataset.datasetEntries):
            mapping.update({index: e.map2d_3d_np(self.dataset.points3D_mapped, zipped=False)})
        return mapping

    def _prepare_cameras(self):
        mapping = {}
        for index, e in enumerate(self.dataset.datasetEntries):
            mapping.update({index: e.camera})
        return mapping

    def _prepare_colmap_cameras(self, mapping_cameras):
        mapping = {}
        for image_id, c in list(mapping_cameras.items()):
            # Note: by default colmap uses focal length of 1.2*max(width, height) to start
            colmap_camera = pycolmap.Camera(model="PINHOLE", width=c.width, height=c.height,
                                            # TODO: could be radial with s
                                            params=[c.camera_intrinsics.focal_x, c.camera_intrinsics.focal_y,
                                                    c.camera_intrinsics.center_x, c.camera_intrinsics.center_y])
            mapping[image_id] = colmap_camera
        return mapping

    def benchmark_absolute_pose(self, tvecs, qvecs, p2d_list, p3d_list, inlier_mask_list, camera_list,
                                absolute_pose_refinement_options):
        assert len(p2d_list) == len(p3d_list) == len(camera_list)
        times = []
        outputs = []
        for index in range(len(p2d_list)):
            start = time.perf_counter()
            o = pycolmap.pose_refinement(
                tvec=tvecs[index],
                qvec=qvecs[index],
                points2D=p2d_list[index],
                points3D=p3d_list[index],
                inlier_mask=inlier_mask_list[index],
                camera=camera_list[index],
                refinement_options=absolute_pose_refinement_options
            )
            times.append(time.perf_counter() - start)
            outputs.append(o)
        return outputs, times

    def validate_output(self, output, camera_poses_list, validation_error_position, validation_error_rotation):
        output_camera_poses = list(map(  # TODO: Remove this function
            lambda x: CameraPose.from_string_wxyz_quaternion_translation(
                string=f"{x.get('qvec')[0]} "
                       f"{x.get('qvec')[1]} "
                       f"{x.get('qvec')[2]} "
                       f"{x.get('qvec')[3]} "
                       f"{x.get('tvec')[0]} "
                       f"{x.get('tvec')[1]} "
                       f"{x.get('tvec')[2]}",
                coordinate_system=CoordinateSystem.COLMAP,
                direction=TransformationDirection.W2C),
            output)
        )

        expected_result = list(zip(camera_poses_list, output_camera_poses))
        position_errors = np.array(
            list(map(lambda cp1_cp2: CameraPose.compute_position_error(cp1_cp2[0], cp1_cp2[1]), expected_result))
        )
        rotation_errors = np.array(
            list(map(lambda cp1_cp2: CameraPose.compute_rotation_error(cp1_cp2[0], cp1_cp2[1]), expected_result))
        )

        assert all(map(lambda o: o["success"], output))
        #  assert np.max(position_errors) <= validation_error_position
        #  assert np.max(rotation_errors) <= validation_error_rotation

    @staticmethod
    def _parse_colmap_output(output):
        return list(map(
            lambda x: CameraPose.from_string_wxyz_quaternion_translation(
                string=f"{x.get('qvec')[0]} "
                       f"{x.get('qvec')[1]} "
                       f"{x.get('qvec')[2]} "
                       f"{x.get('qvec')[3]} "
                       f"{x.get('tvec')[0]} "
                       f"{x.get('tvec')[1]} "
                       f"{x.get('tvec')[2]}",
                coordinate_system=CoordinateSystem.COLMAP,
                direction=TransformationDirection.W2C),
            output)
        )

    def benchmark(self, add_noise=True, refine_focal_length=True,
                  validate_result=True, validation_error_position=5e-02,  # TODO: Remove validation stuff
                  validation_error_rotation=1e-02):

        # TODO: Different ids could be a problem, perhaps switch to datasetEntry.identifier-based mapping
        mapping_2d_3d_by_id = self._prepare_dataset()
        mapping_cameras_by_id = self._prepare_cameras()
        mapping_colmap_cameras_by_id = self._prepare_colmap_cameras(mapping_cameras_by_id)

        absolute_pose_refinement_options = AbsolutePoseRefinementOptions()
        absolute_pose_refinement_options.refine_extra_params = refine_focal_length
        absolute_pose_refinement_options.refine_focal_length = refine_focal_length
        absolute_pose_refinement_options.print_summary = False  # Set to false for now

        """input preparation"""
        tvecs, qvecs, p2d_list, p3d_list, inlier_mask_list, colmap_camera_list, camera_poses_list = [], [], [], [], [], [], []
        # Note: sorting just for peace of mind
        for index, v in sorted(list(mapping_2d_3d_by_id.items()), key=lambda x: x[0]):
            p2d_list.append(v[0])
            p3d_list.append(v[1])
            colmap_camera_list.append(mapping_colmap_cameras_by_id.get(index))
            camera_poses_list.append(mapping_cameras_by_id.get(index).camera_pose)
            tvecs.append(camera_poses_list[index].translation)
            qvecs.append(camera_poses_list[index].wxyz_quaternion)
            inlier_mask_list.append([True for _ in v[0]])  # Set all points to be inliers since it's close enough to GT
            # inlier_mask_list.append(mapping_cameras_by_id.get(index).compute_inlier_mask_mod(
            #     v[0],
            #     v[1],
            #     #  RANSACOptions().max_error
            # ))

        """benchmark"""
        elapsed_time = 0.0
        output, times = self.benchmark_absolute_pose(tvecs, qvecs, p2d_list, p3d_list, inlier_mask_list,
                                                     colmap_camera_list, absolute_pose_refinement_options)
        elapsed_time += sum(times)

        """parsing output"""
        parsed_camera_pose = self._parse_colmap_output(output)
        # TODO: BIG NOTE: Since there is a bug(?) with the camera intrinsics we use the same as before(!!!!!)
        camera_mapping = {
            index: Camera(
                camera_pose=camera_pose,
                camera_intrinsics=mapping_cameras_by_id.get(index).camera_intrinsics,  # TODO: FIX THIS(!!)
                width=mapping_cameras_by_id.get(index).width,  # TODO: FIX THIS(!!)
                height=mapping_cameras_by_id.get(index).height  # TODO: FIX THIS(!!)
            ) for index, camera_pose in enumerate(parsed_camera_pose)
        }

        # if validate_result:
        #     print("validation run...")
        #     output = self.benchmark_absolute_pose(tvecs, qvecs, p2d_list, p3d_list, inlier_mask_list,
        #                                           colmap_camera_list, absolute_pose_refinement_options)
        #     self.validate_output(output, camera_poses_list, validation_error_position, validation_error_rotation)

        self._results = SinglePoseBenchmarkResults(camera_mapping=camera_mapping)
        self._time = elapsed_time
        self._single_times = times


if __name__ == "__main__":
    path = os.path.join(DATASETS_PATH, "reichstag/sparse/TXT")
    image_path = os.path.join(DATASETS_PATH, "reichstag/images")
    ds = load_colmap_dataset(path, image_path, binary=False)

    colmapSinglePoseBenchmark = ColmapSinglePoseBenchmark(ds)
    colmapSinglePoseBenchmark.benchmark()
    time2 = colmapSinglePoseBenchmark.time
    print("finished")
