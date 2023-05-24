import jax.numpy as jnp
from jax import vmap, jit
from jaxopt import LevenbergMarquardt, OptaxSolver

from jax.experimental import sparse

from triangulation_relaxations import so3


# @jit
def project_single_point_gpu(tree_arg):
    E, x, K = tree_arg
    return K @ E @ x


# project_point_vmap = jit(
#     vmap(
#         jit(vmap(project_single_point_gpu, in_axes=((None, 0, None),))),
#         in_axes=((0, 0, 0),),
#     )
# )

project_point_vmap_2 = vmap(project_single_point_gpu, in_axes=((None, 0, None),))


def project_point_vmap_1(tree_arg):
    E, x, K = tree_arg
    # print(E, x, K)
    ke = sparse.bcoo_dot_general(K, E, dimension_numbers=(([1], [0]), ([], [])))
    kex = sparse.bcoo_dot_general(ke, x.T, dimension_numbers=(([1], [0]), ([], []))).T
    # print("kex type: ", kex)
    return kex
    # return project_point_vmap_2(tree_arg)


project_point_vmap = vmap(project_point_vmap_1, in_axes=((0, 0, 0),))

# project_point_vmap = jit(vmap(project_single_point_gpu, in_axes=((None, 0, None),)))

# pose array, 3d points array, indexing array, observations


# @jit
def reproject_gpu(points: jnp.array, pose: jnp.array, K: jnp.array):
    _pose = jnp.linalg.inv(pose)
    KE = jnp.einsum("ijk,ijk->ijk", K, _pose)
    # x = sparse.bcoo_dot_general(points, KE, dimension_numbers=(([2], [2]), ([0], [0])))
    # print(x.shape)
    x = jnp.einsum("ijk,ihk->ihj", KE, points)

    # print("inputs:", (_pose.shape, points.shape, K.shape))
    # x = project_point_vmap((_pose, points, K))
    # print("poject_point:", x)
    x = x[..., :2] / x[..., 2:3]
    return x


# @jit
def rotvec_to_r_gpu(rodrigues_vec):
    theta = jnp.linalg.norm(rodrigues_vec)
    r = rodrigues_vec / theta
    I = jnp.eye(3, dtype=float)
    r_rT = jnp.outer(r, r)
    r_cross = jnp.cross(jnp.eye(3), r)
    return jnp.cos(theta) * I + (1 - jnp.cos(theta)) * r_rT + jnp.sin(theta) * r_cross


# @jit
@vmap
def x_to_pose_gpu(x):
    R = rotvec_to_r_gpu(x[:3])
    return jnp.block([[R, x[3:, jnp.newaxis]], [jnp.zeros(3).T, 1]])


def pose_to_x_gpu(pose):
    return jnp.concatenate([so3.r_to_rotvec(pose.R), pose.t])


# @jit
def get_reprojection_residuals_gpu(pose, points, observations, intrinsics):
    _pose = pose.reshape((-1, 6))
    # _pose = sparse.bcoo_update_layout(_pose, n_batch=1, on_inefficient=None)
    _pose = x_to_pose_gpu(_pose)
    reprojected_points = reproject_gpu(points, _pose, intrinsics)
    # print("repoject_point:", reprojected_points.shape)
    # print(
    #     "output shape:",
    #     ((observations - reprojected_points) ** 2).sum(axis=[0, 2]),
    # )
    ind = jnp.any(observations, axis=2)
    print(
        jnp.where(
            ind, ((observations - reprojected_points) ** 2).sum(axis=[0, 2]), 0
        ).shape
    )
    print(((observations - reprojected_points) ** 2).sum(axis=[0, 2]).shape)
    return jnp.where(
        ind, ((observations - reprojected_points) ** 2).sum(axis=[0, 2]), 0
    )


lm = LevenbergMarquardt(
    residual_fun=get_reprojection_residuals_gpu,
    tol=1e-15,
    gtol=1e-15,
    jit=True,
    solver="inv",
    # verbose=True,
)

jitted_lm = jit(lm.run)


def compile_lm_gpu(_pose0, points_gpu, observations_gpu, intrinsics_gpu):
    _points_gpu = sparse.BCOO.fromdense(jnp.zeros(points_gpu.shape), n_batch=1)
    _observations_gpu = sparse.BCOO.fromdense(
        jnp.zeros(observations_gpu.shape), n_batch=1
    )
    _points_gpu = jnp.zeros(points_gpu.shape)
    _observations_gpu = jnp.zeros(observations_gpu.shape)
    _intrinsics_gpu = jnp.zeros(intrinsics_gpu.shape)
    print(
        "pose.shape: ",
        _pose0.shape,
        "points_gpu.shape: ",
        _points_gpu.shape,
        "observations_gpu.shape: ",
        _observations_gpu.shape,
        "intrinsics_gpu.shape: ",
        _intrinsics_gpu.shape,
    )
    jitted_lm(
        _pose0,
        points=_points_gpu,
        observations=_observations_gpu,
        intrinsics=_intrinsics_gpu,
    ).params.block_until_ready()


def run_lm_gpu(_pose0, points_gpu, observations_gpu, intrinsics_gpu):
    # _points_gpu = sparse.BCOO.fromdense(points_gpu, n_batch=1)
    # _observations_gpu = sparse.BCOO.fromdense(observations_gpu, n_batch=1)
    _points_gpu = points_gpu
    _observations_gpu = observations_gpu
    _intrinsics_gpu = intrinsics_gpu
    return jitted_lm(
        _pose0,
        points=_points_gpu,
        observations=_observations_gpu,
        intrinsics=_intrinsics_gpu,
    ).params
