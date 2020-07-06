# This file is part of project Sverchok. It's copyrighted by the contributors
# recorded in the version control history of the file, available from
# its original location https://github.com/nortikin/sverchok/commit/master
#  
# SPDX-License-Identifier: GPL3
# License-Filename: LICENSE

import numpy as np
from math import pi, cos, sin
from collections import defaultdict

from mathutils import Matrix, Vector

from sverchok.utils.math import (
    ZERO, FRENET, HOUSEHOLDER, TRACK, DIFF, TRACK_NORMAL
    )
from sverchok.utils.geom import LineEquation, rotate_vector_around_vector, autorotate_householder, autorotate_track, autorotate_diff
from sverchok.utils.curve import (
        SvFlipCurve, SvNormalTrack, SvCircle,
        MathutilsRotationCalculator, DifferentialRotationCalculator
    )
from sverchok.utils.surface.core import SvSurface
from sverchok.utils.surface.data import *

def rotate_vector_around_vector_np(v, k, theta):
    """
    Rotate vector v around vector k by theta angle.
    input: v, k - np.array of shape (3,); theta - float, in radians.
    output: np.array.

    This implements Rodrigues' formula: https://en.wikipedia.org/wiki/Rodrigues%27_rotation_formula
    """
    if not isinstance(v, np.ndarray):
        v = np.array(v)
    if not isinstance(k, np.ndarray):
        k = np.array(k)
    if k.ndim == 1:
        k = k[np.newaxis]
    k = k / np.linalg.norm(k, axis=1)

    if isinstance(theta, np.ndarray):
        ct, st = np.cos(theta)[np.newaxis].T, np.sin(theta)[np.newaxis].T
    else:
        ct, st = cos(theta), sin(theta)

    s1 = ct * v
    s2 = st * np.cross(k, v)
    p1 = 1.0 - ct
    p2 = np.apply_along_axis(lambda vi : k.dot(vi), 1, v)
    s3 = p1 * p2 * k
    return s1 + s2 + s3

class SurfaceCurvatureCalculator(object):
    """
    This class contains pre-calculated first and second surface derivatives,
    and calculates any curvature information from them.
    """
    def __init__(self, us, vs, order=True):
        self.us = us
        self.vs = vs
        self.order = order
        self.fu = self.fv = None
        self.duu = self.dvv = self.duv = None
        self.nuu = self.nvv = self.nuv = None
        self.points = None
        self.normals = None

    def set(self, points, normals, fu, fv, duu, dvv, duv, nuu, nvv, nuv):
        """Set derivatives information"""
        self.points = points
        self.normals = normals
        self.fu = fu   # df/du
        self.fv = fv   # df/dv
        self.duu = duu # (fu, fv), a.k.a. E
        self.dvv = dvv # (fv, fv), a.k.a. G
        self.duv = duv # (fu, fv), a.k.a F
        self.nuu = nuu # (fuu, normal), a.k.a l
        self.nvv = nvv # (fvv, normal), a.k.a n
        self.nuv = nuv # (fuv, normal), a.k.a m

    def mean(self):
        """Calculate mean curvature"""
        duu, dvv, duv, nuu, nvv, nuv = self.duu, self.dvv, self.duv, self.nuu, self.nvv, self.nuv
        A = duu*dvv - duv*duv
        B = duu*nvv - 2*duv*nuv + dvv*nuu
        return -B / (2*A)

    def gauss(self):
        """Calculate Gaussian curvature"""
        duu, dvv, duv, nuu, nvv, nuv = self.duu, self.dvv, self.duv, self.nuu, self.nvv, self.nuv
        numerator = nuu * nvv - nuv*nuv
        denominator = duu * dvv - duv*duv
        return numerator / denominator

    def values(self):
        """
        Calculate two principal curvature values.
        If "order" parameter is set to True, then it will be guaranteed,
        that C1 value is always less than C2.
        """
        # It is possible to calculate principal curvature values
        # as solutions of quadratic equation, without calculating
        # corresponding principal curvature directions.

        # lambda^2 (E G - F^2) - lambda (E N - 2 F M + G L) + (L N - M^2) = 0

        duu, dvv, duv, nuu, nvv, nuv = self.duu, self.dvv, self.duv, self.nuu, self.nvv, self.nuv
        A = duu*dvv - duv*duv
        B = duu*nvv - 2*duv*nuv + dvv*nuu
        C = nuu*nvv - nuv*nuv
        D = B*B - 4*A*C
        c1 = (-B - np.sqrt(D))/(2*A)
        c2 = (-B + np.sqrt(D))/(2*A)

        c1[np.isnan(c1)] = 0
        c2[np.isnan(c2)] = 0

        c1mask = (c1 < c2)
        c2mask = np.logical_not(c1mask)

        c1_r = np.where(c1mask, c1, c2)
        c2_r = np.where(c2mask, c1, c2)

        return c1_r, c2_r

    def values_and_directions(self):
        """
        Calculate principal curvature values together with principal curvature directions.
        If "order" parameter is set to True, then it will be guaranteed, that C1 value
        is always less than C2. Curvature directions are always output correspondingly,
        i.e. principal_direction_1 corresponds to principal_value_1 and principal_direction_2
        corresponds to principal_value_2.
        """
        # If we need not only curvature values, but principal curvature directions as well,
        # we have to solve an eigenvalue problem to find values and directions at once.

        # L p = lambda G p

        fu, fv = self.fu, self.fv
        duu, dvv, duv, nuu, nvv, nuv = self.duu, self.dvv, self.duv, self.nuu, self.nvv, self.nuv
        n = len(self.us)

        L = np.empty((n,2,2))
        L[:,0,0] = nuu
        L[:,0,1] = nuv
        L[:,1,0] = nuv
        L[:,1,1] = nvv

        G = np.empty((n,2,2))
        G[:,0,0] = duu
        G[:,0,1] = duv
        G[:,1,0] = duv
        G[:,1,1] = dvv

        M = np.matmul(np.linalg.inv(G), L)
        eigvals, eigvecs = np.linalg.eig(M)
        # Values of first and second principal curvatures
        c1 = eigvals[:,0]
        c2 = eigvals[:,1]

        if self.order:
            c1mask = (c1 < c2)
            c2mask = np.logical_not(c1mask)
            c1_r = np.where(c1mask, c1, c2)
            c2_r = np.where(c2mask, c1, c2)
        else:
            c1_r = c1
            c2_r = c2

        # dir_1 corresponds to c1, dir_2 corresponds to c2
        dir_1_x = eigvecs[:,0,0][np.newaxis].T
        dir_2_x = eigvecs[:,0,1][np.newaxis].T
        dir_1_y = eigvecs[:,1,0][np.newaxis].T
        dir_2_y = eigvecs[:,1,1][np.newaxis].T

        # another possible approach
#         A = duv * nvv - dvv*nuv 
#         B = duu * nvv - dvv*nuu
#         C = duu*nuv - duv*nuu
#         D = B*B - 4*A*C
#         t1 = (-B - np.sqrt(D)) / (2*A)
#         t2 = (-B + np.sqrt(D)) / (2*A)

        dir_1 = dir_1_x * fu + dir_1_y * fv
        dir_2 = dir_2_x * fu + dir_2_y * fv

        dir_1 = dir_1 / np.linalg.norm(dir_1, axis=1, keepdims=True)
        dir_2 = dir_2 / np.linalg.norm(dir_2, axis=1, keepdims=True)

        if self.order:
            c1maskT = c1mask[np.newaxis].T
            c2maskT = c2mask[np.newaxis].T
            dir_1_r = np.where(c1maskT, dir_1, -dir_2)
            dir_2_r = np.where(c2maskT, dir_1, dir_2)
        else:
            dir_1_r = dir_1
            dir_2_r = dir_2
        #r = (np.cross(dir_1_r, dir_2_r) * self.normals).sum(axis=1)
        #print(r)

        dir1_uv = eigvecs[:,:,0]
        dir2_uv = eigvecs[:,:,1]
        if self.order:
            c1maskT = c1mask[np.newaxis].T
            c2maskT = c2mask[np.newaxis].T
            dir1_uv_r = np.where(c1maskT, dir1_uv, -dir2_uv)
            dir2_uv_r = np.where(c2maskT, dir1_uv, dir2_uv)
        else:
            dir1_uv_r = dir1_uv
            dir2_uv_r = dir2_uv
            
        return c1_r, c2_r, dir1_uv_r, dir2_uv_r, dir_1_r, dir_2_r

    def calc(self, need_values=True, need_directions=True, need_uv_directions = False, need_gauss=True, need_mean=True, need_matrix = True):
        """
        Calculate curvature information.
        Return value: SurfaceCurvatureData instance.
        """
        # We try to do as less calculations as possible,
        # by not doing complex computations if not required
        # and reusing results of other computations if possible.
        data = SurfaceCurvatureData()
        if need_matrix:
            need_directions = True
        if need_uv_directions:
            need_directions = True
        if need_directions:
            # If we need principal curvature directions, then the method
            # being used will calculate us curvature values for free.
            c1, c2, dir1_uv, dir2_uv, dir1, dir2 = self.values_and_directions()
            data.principal_value_1, data.principal_value_2 = c1, c2
            data.principal_direction_1, data.principal_direction_2 = dir1, dir2
            data.principal_direction_1_uv = dir1_uv
            data.principal_direction_2_uv = dir2_uv
            if need_gauss:
                data.gauss = c1 * c2
            if need_mean:
                data.mean = (c1 + c2)/2.0
        if need_matrix:
            matrices_np = np.dstack((data.principal_direction_2, data.principal_direction_1, self.normals))
            matrices_np = np.transpose(matrices_np, axes=(0,2,1))
            matrices_np = np.linalg.inv(matrices_np)
            matrices = [Matrix(m.tolist()).to_4x4() for m in matrices_np]
            for matrix, point in zip(matrices, self.points):
                matrix.translation = Vector(point)
            data.matrix = matrices
        if need_values and not need_directions:
            c1, c2 = self.values()
            data.principal_value_1, data.principal_value_2 = c1, c2
            if need_gauss:
                data.gauss = c1 * c2
            if need_mean:
                data.mean = (c1 + c2)/2.0
        if need_gauss and not need_directions and not need_values:
            data.gauss = self.gauss()
        if need_mean and not need_directions and not need_values:
            data.mean = self.mean()
        return data

class SvInterpolatingSurface(SvSurface):
    __description__ = "Interpolating"

    def __init__(self, u_bounds, v_bounds, u_spline_constructor, v_splines):
        self.v_splines = v_splines
        self.u_spline_constructor = u_spline_constructor
        self.u_bounds = u_bounds
        self.v_bounds = v_bounds

        # Caches
        # v -> Spline
        self._u_splines = {}
        # (u,v) -> vertex
        self._eval_cache = {}
        # (u,v) -> normal
        self._normal_cache = {}

    @property
    def u_size(self):
        return self.u_bounds[1] - self.u_bounds[0]
        #v = 0.0
        #verts = [spline.evaluate(v) for spline in self.v_splines]
        #return self.get_u_spline(v, verts).u_size

    @property
    def v_size(self):
        return self.v_bounds[1] - self.v_bounds[0]
        #return self.v_splines[0].v_size

    def get_u_spline(self, v, vertices):
        """Get a spline along U direction for specified value of V coordinate"""
        spline = self._u_splines.get(v, None)
        if spline is not None:
            return spline
        else:
            spline = self.u_spline_constructor(vertices)
            self._u_splines[v] = spline
            return spline

    def _evaluate(self, u, v):
        spline_vertices = []
        for spline in self.v_splines:
            v_min, v_max = spline.get_u_bounds()
            vx = (v_max - v_min) * v + v_min
            point = spline.evaluate(vx)
            spline_vertices.append(point)
        #spline_vertices = [spline.evaluate(v) for spline in self.v_splines]
        u_spline = self.get_u_spline(v, spline_vertices)
        result = u_spline.evaluate(u)
        return result

    def evaluate(self, u, v):
        result = self._eval_cache.get((u,v), None)
        if result is not None:
            return result
        else:
            result = self._evaluate(u, v)
            self._eval_cache[(u,v)] = result
            return result

#     def evaluate_array(self, us, vs):
#         # FIXME: To be optimized!
#         normals = [self._evaluate(u, v) for u,v in zip(us, vs)]
#         return np.array(normals)

    def evaluate_array(self, us, vs):
        result = np.empty((len(us), 3))
        v_to_u = defaultdict(list)
        v_to_i = defaultdict(list)
        for i, (u, v) in enumerate(zip(us, vs)):
            v_to_u[v].append(u)
            v_to_i[v].append(i)
        for v, us_by_v in v_to_u.items():
            is_by_v = v_to_i[v]
            spline_vertices = []
            for spline in self.v_splines:
                v_min, v_max = spline.get_u_bounds()
                vx = (v_max - v_min) * v + v_min
                point = spline.evaluate(vx)
                spline_vertices.append(point)
            u_spline = self.get_u_spline(v, spline_vertices)
            points = u_spline.evaluate_array(np.array(us_by_v))
            idxs = np.array(is_by_v)[np.newaxis].T
            np.put_along_axis(result, idxs, points, axis=0)
        return result

    def _normal(self, u, v):
        h = 0.001
        point = self.evaluate(u, v)
        # we know this exists because it was filled in evaluate()
        u_spline = self._u_splines[v]
        u_tangent = u_spline.tangent(u)
        point_v = self.evaluate(u, v+h)
        dv = (point_v - point)/h
        n = np.cross(u_tangent, dv)
        norm = np.linalg.norm(n)
        if norm != 0:
            n = n / norm
        return n

    def normal(self, u, v):
        result = self._normal_cache.get((u,v), None)
        if result is not None:
            return result
        else:
            result = self._normal(u, v)
            self._normal_cache[(u,v)] = result
            return result

#     def normal_array(self, us, vs):
#         # FIXME: To be optimized!
#         normals = [self._normal(u, v) for u,v in zip(us, vs)]
#         return np.array(normals)

    def normal_array(self, us, vs):
        h = 0.001
        result = np.empty((len(us), 3))
        v_to_u = defaultdict(list)
        v_to_i = defaultdict(list)
        for i, (u, v) in enumerate(zip(us, vs)):
            v_to_u[v].append(u)
            v_to_i[v].append(i)
        for v, us_by_v in v_to_u.items():
            us_by_v = np.array(us_by_v)
            is_by_v = v_to_i[v]
            spline_vertices = []
            spline_vertices_h = []
            for v_spline in self.v_splines:
                v_min, v_max = v_spline.get_u_bounds()
                vx = (v_max - v_min) * v + v_min
                point = v_spline.evaluate(vx)
                point_h = v_spline.evaluate(vx + h)
                spline_vertices.append(point)
                spline_vertices_h.append(point_h)
            u_spline = self.get_u_spline(v, spline_vertices)
            u_spline_h = self.get_u_spline(v+h, spline_vertices_h)
            points = u_spline.evaluate_array(us_by_v)
            points_v_h = u_spline_h.evaluate_array(us_by_v)
            points_u_h = u_spline.evaluate_array(us_by_v + h)
            dvs = (points_v_h - points) / h
            dus = (points_u_h - points) / h
            normals = np.cross(dus, dvs)
            norms = np.linalg.norm(normals, axis=1, keepdims=True)
            normals = normals / norms

            idxs = np.array(is_by_v)[np.newaxis].T
            np.put_along_axis(result, idxs, normals, axis=0)
        return result

PROJECT = 'project'
COPROJECT = 'coproject'

def _dot(vs1, vs2):
    return (vs1 * vs2).sum(axis=1)[np.newaxis].T

class SvDeformedByFieldSurface(SvSurface):
    def __init__(self, surface, field, coefficient=1.0, by_normal=None):
        self.surface = surface
        self.field = field
        self.coefficient = coefficient
        self.by_normal = by_normal
        self.normal_delta = 0.001
        self.__description__ = "{}({})".format(field, surface)

    def get_coord_mode(self):
        return self.surface.get_coord_mode()

    def get_u_min(self):
        return self.surface.get_u_min()

    def get_u_max(self):
        return self.surface.get_u_max()

    def get_v_min(self):
        return self.surface.get_v_min()

    def get_v_max(self):
        return self.surface.get_v_max()

    @property
    def u_size(self):
        return self.surface.u_size

    @property
    def v_size(self):
        return self.surface.v_size

    @property
    def has_input_matrix(self):
        return self.surface.has_input_matrix

    def get_input_matrix(self):
        return self.surface.get_input_matrix()

    def evaluate(self, u, v):
        p = self.surface.evaluate(u, v)
        vec = self.field.evaluate(p[0], p[1], p[2])
        if self.by_normal == PROJECT:
            normal = self.surface.normal(u, v)
            vec = np.dot(vec, normal) * normal / np.dot(normal, normal)
        elif self.by_normal == COPROJECT:
            normal = self.surface.normal(u, v)
            projection = np.dot(vec, normal) * normal / np.dot(normal, normal)
            vec = vec - projection
        return p + self.coefficient * vec

    def evaluate_array(self, us, vs):
        ps = self.surface.evaluate_array(us, vs)
        xs, ys, zs = ps[:,0], ps[:,1], ps[:,2]
        vxs, vys, vzs = self.field.evaluate_grid(xs, ys, zs)
        vecs = np.stack((vxs, vys, vzs)).T
        if self.by_normal == PROJECT:
            normals = self.surface.normal_array(us, vs)
            vecs = _dot(vecs, normals) * normals / _dot(normals, normals)
        elif self.by_normal == COPROJECT:
            normals = self.surface.normal_array(us, vs)
            projections = _dot(vecs, normals) * normals / _dot(normals, normals)
            vecs = vecs - projections
        return ps + self.coefficient * vecs

    def normal(self, u, v):
        h = self.normal_delta
        p = self.evaluate(u, v)
        p_u = self.evaluate(u+h, v)
        p_v = self.evaluate(u, v+h)
        du = (p_u - p) / h
        dv = (p_v - p) / h
        normal = np.cross(du, dv)
        n = np.linalg.norm(normal)
        normal = normal / n
        return normal

    def normal_array(self, us, vs):
        surf_vertices = self.evaluate_array(us, vs)
        u_plus = self.evaluate_array(us + self.normal_delta, vs)
        v_plus = self.evaluate_array(us, vs + self.normal_delta)
        du = u_plus - surf_vertices
        dv = v_plus - surf_vertices
        #self.info("Du: %s", du)
        #self.info("Dv: %s", dv)
        normal = np.cross(du, dv)
        norm = np.linalg.norm(normal, axis=1)[np.newaxis].T
        #if norm != 0:
        normal = normal / norm
        #self.info("Normals: %s", normal)
        return normal

class SvRevolutionSurface(SvSurface):
    __description__ = "Revolution"

    def __init__(self, curve, point, direction, global_origin=True):
        self.curve = curve
        self.point = point
        self.direction = direction
        self.global_origin = global_origin
        self.normal_delta = 0.001
        self.v_bounds = (0.0, 2*pi)

    def evaluate(self, u, v):
        point_on_curve = self.curve.evaluate(u)
        dv = point_on_curve - self.point
        result = np.array(rotate_vector_around_vector(dv, self.direction, v))
        if not self.global_origin:
            result = result + self.point
        return result

    def evaluate_array(self, us, vs):
        points_on_curve = self.curve.evaluate_array(us)
        dvs = points_on_curve - self.point
        result = rotate_vector_around_vector_np(dvs, self.direction, vs)
        if not self.global_origin:
            result = result + self.point
        return result

    def get_u_min(self):
        return self.curve.get_u_bounds()[0]

    def get_u_max(self):
        return self.curve.get_u_bounds()[1]

    def get_v_min(self):
        return self.v_bounds[0]

    def get_v_max(self):
        return self.v_bounds[1]

class SvExtrudeCurveVectorSurface(SvSurface):
    def __init__(self, curve, vector):
        self.curve = curve
        self.vector = np.array(vector)
        self.normal_delta = 0.001
        self.__description__ = "Extrusion of {}".format(curve)

    def evaluate(self, u, v):
        point_on_curve = self.curve.evaluate(u)
        return point_on_curve + v * self.vector

    def evaluate_array(self, us, vs):
        points_on_curve = self.curve.evaluate_array(us)
        return points_on_curve + vs[np.newaxis].T * self.vector

    def get_u_min(self):
        return self.curve.get_u_bounds()[0]

    def get_u_max(self):
        return self.curve.get_u_bounds()[1]

    def get_v_min(self):
        return 0.0

    def get_v_max(self):
        return 1.0

    @property
    def u_size(self):
        m,M = self.curve.get_u_bounds()
        return M - m

    @property
    def v_size(self):
        return 1.0

class SvExtrudeCurvePointSurface(SvSurface):
    def __init__(self, curve, point):
        self.curve = curve
        self.point = point
        self.normal_delta = 0.001
        self.__description__ = "Extrusion of {}".format(curve)

    def evaluate(self, u, v):
        point_on_curve = self.curve.evaluate(u)
        return (1.0 - v) * point_on_curve + v * self.point

    def evaluate_array(self, us, vs):
        points_on_curve = self.curve.evaluate_array(us)
        vs = vs[np.newaxis].T
        return (1.0 - vs) * points_on_curve + vs * self.point

    def get_u_min(self):
        return self.curve.get_u_bounds()[0]

    def get_u_max(self):
        return self.curve.get_u_bounds()[1]

    def get_v_min(self):
        return 0.0

    def get_v_max(self):
        return 1.0

    @property
    def u_size(self):
        m,M = self.curve.get_u_bounds()
        return M - m

    @property
    def v_size(self):
        return 1.0

PROFILE = 'profile'
EXTRUSION = 'extrusion'

class SvExtrudeCurveCurveSurface(SvSurface):
    def __init__(self, u_curve, v_curve, origin = PROFILE):
        self.u_curve = u_curve
        self.v_curve = v_curve
        self.origin = origin
        self.normal_delta = 0.001
        self.__description__ = "Extrusion of {}".format(u_curve)

    def evaluate(self, u, v):
        u_point = self.u_curve.evaluate(u)
        u_min, u_max = self.u_curve.get_u_bounds()
        v_min, v_max = self.v_curve.get_u_bounds()
        v0 = self.v_curve.evaluate(v_min)
        v_point = self.v_curve.evaluate(v)
        if self.origin == EXTRUSION:
            result = u_point + v_point
        else:
            result = u_point + (v_point - v0)
        return result

    def evaluate_array(self, us, vs):
        u_points = self.u_curve.evaluate_array(us)
        u_min, u_max = self.u_curve.get_u_bounds()
        v_min, v_max = self.v_curve.get_u_bounds()
        v0 = self.v_curve.evaluate(v_min)
        v_points = self.v_curve.evaluate_array(vs)
        if self.origin == EXTRUSION:
            result = u_points + v_points
        else:
            result = u_points + (v_points - v0)
        return result

    def get_u_min(self):
        return self.u_curve.get_u_bounds()[0]

    def get_u_max(self):
        return self.u_curve.get_u_bounds()[1]

    def get_v_min(self):
        return self.v_curve.get_u_bounds()[0]

    def get_v_max(self):
        return self.v_curve.get_u_bounds()[1]

    @property
    def u_size(self):
        m,M = self.u_curve.get_u_bounds()
        return M - m

    @property
    def v_size(self):
        m,M = self.v_curve.get_u_bounds()
        return M - m

class SvExtrudeCurveFrenetSurface(SvSurface):
    def __init__(self, profile, extrusion, origin = PROFILE):
        self.profile = profile
        self.extrusion = extrusion
        self.origin = origin
        self.normal_delta = 0.001
        self.__description__ = "Extrusion of {}".format(profile)

    def evaluate(self, u, v):
        return self.evaluate_array(np.array([u]), np.array([v]))[0]

    def evaluate_array(self, us, vs):
        profile_points = self.profile.evaluate_array(us)
        u_min, u_max = self.profile.get_u_bounds()
        v_min, v_max = self.extrusion.get_u_bounds()
        profile_vectors = profile_points
        profile_vectors = np.transpose(profile_vectors[np.newaxis], axes=(1, 2, 0))
        extrusion_start = self.extrusion.evaluate(v_min)
        extrusion_points = self.extrusion.evaluate_array(vs)
        extrusion_vectors = extrusion_points - extrusion_start
        frenet, _ , _ = self.extrusion.frame_array(vs)
        profile_vectors = (frenet @ profile_vectors)[:,:,0]
        result = extrusion_vectors + profile_vectors
        if self.origin == EXTRUSION:
            result = result + self.extrusion.evaluate(v_min)
        return result

    def get_u_min(self):
        return self.profile.get_u_bounds()[0]

    def get_u_max(self):
        return self.profile.get_u_bounds()[1]

    def get_v_min(self):
        return self.extrusion.get_u_bounds()[0]

    def get_v_max(self):
        return self.extrusion.get_u_bounds()[1]

    @property
    def u_size(self):
        m,M = self.profile.get_u_bounds()
        return M - m

    @property
    def v_size(self):
        m,M = self.extrusion.get_u_bounds()
        return M - m

class SvExtrudeCurveZeroTwistSurface(SvSurface):
    def __init__(self, profile, extrusion, resolution, origin = PROFILE):
        self.profile = profile
        self.extrusion = extrusion
        self.origin = origin
        self.normal_delta = 0.001
        self.extrusion.pre_calc_torsion_integral(resolution)
        self.__description__ = "Extrusion of {}".format(profile)

    def evaluate(self, u, v):
        return self.evaluate_array(np.array([u]), np.array([v]))[0]

    def evaluate_array(self, us, vs):
        profile_points = self.profile.evaluate_array(us)
        u_min, u_max = self.profile.get_u_bounds()
        v_min, v_max = self.extrusion.get_u_bounds()
        profile_vectors = profile_points
        profile_vectors = np.transpose(profile_vectors[np.newaxis], axes=(1, 2, 0))
        extrusion_start = self.extrusion.evaluate(v_min)
        extrusion_points = self.extrusion.evaluate_array(vs)
        extrusion_vectors = extrusion_points - extrusion_start

        frenet, _ , _ = self.extrusion.frame_array(vs)

        angles = - self.extrusion.torsion_integral(vs)
        n = len(us)
        zeros = np.zeros((n,))
        ones = np.ones((n,))
        row1 = np.stack((np.cos(angles), np.sin(angles), zeros)).T # (n, 3)
        row2 = np.stack((-np.sin(angles), np.cos(angles), zeros)).T # (n, 3)
        row3 = np.stack((zeros, zeros, ones)).T # (n, 3)
        rotation_matrices = np.dstack((row1, row2, row3))

        profile_vectors = (frenet @ rotation_matrices @ profile_vectors)[:,:,0]
        result = extrusion_vectors + profile_vectors
        if self.origin == EXTRUSION:
            result = result + self.extrusion.evaluate(v_min)
        return result

    def get_u_min(self):
        return self.profile.get_u_bounds()[0]

    def get_u_max(self):
        return self.profile.get_u_bounds()[1]

    def get_v_min(self):
        return self.extrusion.get_u_bounds()[0]

    def get_v_max(self):
        return self.extrusion.get_u_bounds()[1]

class SvExtrudeCurveTrackNormalSurface(SvSurface):
    def __init__(self, profile, extrusion, resolution, origin = PROFILE):
        self.profile = profile
        self.extrusion = extrusion
        self.origin = origin
        self.normal_delta = 0.001
        self.tracker = SvNormalTrack(extrusion, resolution)
        self.__description__ = "Extrusion of {}".format(profile)

    def get_u_min(self):
        return self.profile.get_u_bounds()[0]

    def get_u_max(self):
        return self.profile.get_u_bounds()[1]

    def get_v_min(self):
        return self.extrusion.get_u_bounds()[0]

    def get_v_max(self):
        return self.extrusion.get_u_bounds()[1]

    def evaluate(self, u, v):
        return self.evaluate_array(np.array([u]), np.array([v]))[0]

    def evaluate_array(self, us, vs):
        profile_vectors = self.profile.evaluate_array(us)
        u_min, u_max = self.profile.get_u_bounds()
        v_min, v_max = self.extrusion.get_u_bounds()
        profile_vectors = np.transpose(profile_vectors[np.newaxis], axes=(1, 2, 0))
        extrusion_start = self.extrusion.evaluate(v_min)
        extrusion_points = self.extrusion.evaluate_array(vs)
        extrusion_vectors = extrusion_points - extrusion_start

        matrices = self.tracker.evaluate_array(vs)
        profile_vectors = (matrices @ profile_vectors)[:,:,0]
        result = extrusion_vectors + profile_vectors
        if self.origin == EXTRUSION:
            result = result + extrusion_start
        return result

class SvExtrudeCurveMathutilsSurface(SvSurface):
    def __init__(self, profile, extrusion, algorithm, orient_axis='Z', up_axis='X', origin = PROFILE):
        self.profile = profile
        self.extrusion = extrusion
        self.algorithm = algorithm
        self.orient_axis = orient_axis
        self.up_axis = up_axis
        self.origin = origin
        self.normal_delta = 0.001
        self.__description__ = "Extrusion of {}".format(profile)

    def evaluate(self, u, v):
        return self.evaluate_array(np.array([u]), np.array([v]))[0]

    def get_matrix(self, tangent):
        x = Vector((1.0, 0.0, 0.0))
        y = Vector((0.0, 1.0, 0.0))
        z = Vector((0.0, 0.0, 1.0))

        if self.orient_axis == 'X':
            ax1, ax2, ax3 = x, y, z
        elif self.orient_axis == 'Y':
            ax1, ax2, ax3 = y, x, z
        else:
            ax1, ax2, ax3 = z, x, y

        if self.algorithm == 'householder':
            rot = autorotate_householder(ax1, tangent).inverted()
        elif self.algorithm == 'track':
            rot = autorotate_track(self.orient_axis, tangent, self.up_axis)
        elif self.algorithm == 'diff':
            rot = autorotate_diff(tangent, ax1)
        else:
            raise Exception("Unsupported algorithm")

        return rot

    def get_matrices(self, vs):
        tangents = self.extrusion.tangent_array(vs)
        matrices = []
        for tangent in tangents:
            matrix = self.get_matrix(Vector(tangent)).to_3x3()
            matrices.append(matrix)
        return np.array(matrices)

    def evaluate_array(self, us, vs):
        profile_points = self.profile.evaluate_array(us)
        u_min, u_max = self.profile.get_u_bounds()
        v_min, v_max = self.extrusion.get_u_bounds()
        profile_vectors = profile_points
        profile_vectors = np.transpose(profile_vectors[np.newaxis], axes=(1, 2, 0))
        extrusion_start = self.extrusion.evaluate(v_min)
        extrusion_points = self.extrusion.evaluate_array(vs)
        extrusion_vectors = extrusion_points - extrusion_start

        matrices = self.get_matrices(vs)

        profile_vectors = (matrices @ profile_vectors)[:,:,0]
        result = extrusion_vectors + profile_vectors
        if self.origin == EXTRUSION:
            result = result + self.extrusion.evaluate(v_min)
        return result

    def get_u_min(self):
        return self.profile.get_u_bounds()[0]

    def get_u_max(self):
        return self.profile.get_u_bounds()[1]

    def get_v_min(self):
        return self.extrusion.get_u_bounds()[0]

    def get_v_max(self):
        return self.extrusion.get_u_bounds()[1]

class SvConstPipeSurface(SvSurface):
    __description__ = "Pipe"

    def __init__(self, curve, radius, algorithm = FRENET, resolution=50):
        self.curve = curve
        self.radius = radius
        self.circle = SvCircle(Matrix(), radius)
        self.algorithm = algorithm
        self.normal_delta = 0.001
        self.u_bounds = self.circle.get_u_bounds()
        if algorithm in {FRENET, ZERO, TRACK_NORMAL}:
            self.calculator = DifferentialRotationCalculator(curve, algorithm, resolution)

    def get_u_min(self):
        return self.u_bounds[0]

    def get_u_max(self):
        return self.u_bounds[1]

    def get_v_min(self):
        return self.curve.get_u_bounds()[0]

    def get_v_max(self):
        return self.curve.get_u_bounds()[1]

    def evaluate(self, u, v):
        return self.evaluate_array(np.array([u]), np.array([v]))[0]

    def get_matrix(self, tangent):
        return MathutilsRotationCalculator.get_matrix(tangent, scale=1.0,
                axis=2,
                algorithm = self.algorithm,
                scale_all=False)

    def get_matrices(self, ts):
        if self.algorithm in {FRENET, ZERO, TRACK_NORMAL}:
            return self.calculator.get_matrices(ts)
        elif self.algorithm in {HOUSEHOLDER, TRACK, DIFF}:
            tangents = self.curve.tangent_array(ts)
            matrices = np.vectorize(lambda t : self.get_matrix(t), signature='(3)->(3,3)')(tangents)
            return matrices
        else:
            raise Exception("Unsupported algorithm")

    def evaluate_array(self, us, vs):
        profile_vectors = self.circle.evaluate_array(us)
        u_min, u_max = self.circle.get_u_bounds()
        v_min, v_max = self.curve.get_u_bounds()
        profile_vectors = np.transpose(profile_vectors[np.newaxis], axes=(1, 2, 0))
        extrusion_start = self.curve.evaluate(v_min)
        extrusion_points = self.curve.evaluate_array(vs)
        extrusion_vectors = extrusion_points - extrusion_start

        matrices = self.get_matrices(vs)

        profile_vectors = (matrices @ profile_vectors)[:,:,0]
        result = extrusion_vectors + profile_vectors
        result = result + extrusion_start
        return result

class SvCurveLerpSurface(SvSurface):
    __description__ = "Lerp"

    def __init__(self, curve1, curve2):
        self.curve1 = curve1
        self.curve2 = curve2
        self.normal_delta = 0.001
        self.v_bounds = (0.0, 1.0)
        self.u_bounds = (0.0, 1.0)
        self.c1_min, self.c1_max = curve1.get_u_bounds()
        self.c2_min, self.c2_max = curve2.get_u_bounds()

    def evaluate(self, u, v):
        return self.evaluate_array(np.array([u]), np.array([v]))[0]

    def evaluate_array(self, us, vs):
        us1 = (self.c1_max - self.c1_min) * us + self.c1_min
        us2 = (self.c2_max - self.c2_min) * us + self.c2_min
        c1_points = self.curve1.evaluate_array(us1)
        c2_points = self.curve2.evaluate_array(us2)
        vs = vs[np.newaxis].T
        points = (1.0 - vs)*c1_points + vs*c2_points
        return points

    def get_u_min(self):
        return self.u_bounds[0]

    def get_u_max(self):
        return self.u_bounds[1]

    def get_v_min(self):
        return self.v_bounds[0]

    def get_v_max(self):
        return self.v_bounds[1]

    @property
    def u_size(self):
        return self.u_bounds[1] - self.u_bounds[0]

    @property
    def v_size(self):
        return self.v_bounds[1] - self.v_bounds[0]

class SvSurfaceLerpSurface(SvSurface):
    __description__ = "Lerp"

    def __init__(self, surface1, surface2, coefficient):
        self.surface1 = surface1
        self.surface2 = surface2
        self.coefficient = coefficient
        self.normal_delta = 0.001
        self.v_bounds = (0.0, 1.0)
        self.u_bounds = (0.0, 1.0)
        self.s1_u_min, self.s1_u_max = surface1.get_u_min(), surface1.get_u_max()
        self.s1_v_min, self.s1_v_max = surface1.get_v_min(), surface1.get_v_max()
        self.s2_u_min, self.s2_u_max = surface2.get_u_min(), surface2.get_u_max()
        self.s2_v_min, self.s2_v_max = surface2.get_v_min(), surface2.get_v_max()

    def get_u_min(self):
        return self.u_bounds[0]

    def get_u_max(self):
        return self.u_bounds[1]

    def get_v_min(self):
        return self.v_bounds[0]

    def get_v_max(self):
        return self.v_bounds[1]

    @property
    def u_size(self):
        return self.u_bounds[1] - self.u_bounds[0]

    @property
    def v_size(self):
        return self.v_bounds[1] - self.v_bounds[0]

    def evaluate(self, u, v):
        return self.evaluate_array(np.array([u]), np.array([v]))[0]
    
    def evaluate_array(self, us, vs):
        us1 = (self.s1_u_max - self.s1_u_min) * us + self.s1_u_min
        us2 = (self.s2_u_max - self.s2_u_min) * us + self.s2_u_min
        vs1 = (self.s1_v_max - self.s1_v_min) * vs + self.s1_v_min
        vs2 = (self.s2_v_max - self.s2_v_min) * vs + self.s2_v_min
        s1_points = self.surface1.evaluate_array(us1, vs1)
        s2_points = self.surface2.evaluate_array(us2, vs2)
        k = self.coefficient
        points = (1.0 - k) * s1_points + k * s2_points
        return points

class SvCoonsSurface(SvSurface):
    __description__ = "Coons Patch"
    def __init__(self, curve1, curve2, curve3, curve4):
        self.curve1 = curve1
        self.curve2 = curve2
        self.curve3 = curve3
        self.curve4 = curve4
        self.linear1 = SvCurveLerpSurface(curve1, SvFlipCurve(curve3))
        self.linear2 = SvCurveLerpSurface(curve2, SvFlipCurve(curve4))
        self.c1_t_min, self.c1_t_max = curve1.get_u_bounds()
        self.c3_t_min, self.c3_t_max = curve3.get_u_bounds()

        self.corner1 = self.curve1.evaluate(self.c1_t_min)
        self.corner2 = self.curve1.evaluate(self.c1_t_max)
        self.corner3 = self.curve3.evaluate(self.c3_t_max)
        self.corner4 = self.curve3.evaluate(self.c3_t_min)

        self.normal_delta = 0.001
    
    def get_u_min(self):
        return 0
    
    def get_u_max(self):
        return 1
    
    def get_v_min(self):
        return 0
    
    def get_v_max(self):
        return 1

    def _calc_b(self, u, v, is_array):
        corner1, corner2, corner3, corner4 = self.corner1, self.corner2, self.corner3, self.corner4
        if is_array:
            u = u[np.newaxis].T
            v = v[np.newaxis].T
        b = (corner1 * (1 - u) * (1 - v) + corner2 * u * (1 - v) + corner3 * (1 - u) * v + corner4 * u * v)
        return b
    
    def evaluate(self, u, v):    
        return self.linear1.evaluate(u, v) + self.linear2.evaluate(v, 1-u) - self._calc_b(u, v, False)
    
    def evaluate_array(self, us, vs):
        return self.linear1.evaluate_array(us, vs) + self.linear2.evaluate_array(vs, 1-us) - self._calc_b(us, vs, True)

class SvTaperSweepSurface(SvSurface):
    __description__ = "Taper & Sweep"

    def __init__(self, profile, taper, point, direction):
        self.profile = profile
        self.taper = taper
        self.direction = direction
        self.point = point
        self.line = LineEquation.from_direction_and_point(direction, point)
        self.normal_delta = 0.001

    def get_u_min(self):
        return self.profile.get_u_bounds()[0]

    def get_u_max(self):
        return self.profile.get_u_bounds()[1]

    def get_v_min(self):
        return self.taper.get_u_bounds()[0]

    def get_v_max(self):
        return self.taper.get_u_bounds()[1]

    def evaluate(self, u, v):
        taper_point = self.taper.evaluate(v)
        taper_projection = np.array( self.line.projection_of_point(taper_point) )
        scale = np.linalg.norm(taper_projection - taper_point)
        profile_point = self.profile.evaluate(u)
        return profile_point * scale + taper_projection

    def evaluate_array(self, us, vs):
        taper_points = self.taper.evaluate_array(vs)
        taper_projections = self.line.projection_of_points(taper_points)
        scale = np.linalg.norm(taper_projections - taper_points, axis=1, keepdims=True)
        profile_points = self.profile.evaluate_array(us)
        return profile_points * scale + taper_projections
