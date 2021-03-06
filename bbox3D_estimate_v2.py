import torch
import numpy as np


def dimensions_to_corners(dimensions):
    """
    Note that the center of the 3D bounding box is the center of the bottom surface,
    not the geometric center of the box, following KITTI's defination.
    """
    h, w, l = dimensions[:, 0], dimensions[:, 1], dimensions[:, 2]
    zeros = torch.zeros(dimensions.size(0)).to(dimensions.device)
    corner_x = torch.stack([ l/2.0, -l/2.0, -l/2.0,  l/2.0,  l/2.0, -l/2.0, -l/2.0,  l/2.0])
    corner_y = torch.stack([ zeros,  zeros,  zeros,  zeros,     -h,     -h,     -h,     -h])
    corner_z = torch.stack([ w/2.0,  w/2.0, -w/2.0, -w/2.0,  w/2.0,  w/2.0, -w/2.0, -w/2.0])
    corners = torch.stack([corner_x, corner_y, corner_z]) # 3x8xN

    return corners.permute(2, 1, 0).contiguous() # Nx8x3


def solve_3d_bbox_single(bbox2D, corners, theta_l, calib):
    """
    Input:
        bbox2D: Tensor(4), [x1, y1, x2, y2]
        corners: Tensor(8, 3), aligned corners without rotation
        theta_l: camera direction [-pi, pi]
        calib: calibration metrices in KITTI
    """

    x1, y1, x2, y2 = bbox2D

    # useful calibrations
    P2 = calib['P2']
    R0_rect = torch.eye(4)
    R0_rect[:3, :3] = calib['R0_rect']
    K = torch.matmul(P2, R0_rect)

    # use 2D bbox to estimate global rotation
    theta_ray = torch.atan2(P2[0, 0], (x1 + x2) * 0.5 - P2[0, 2])
    ry = np.pi - theta_ray - theta_l

    Ry_T = torch.tensor([[ torch.cos(ry), 0.0, -torch.sin(ry)],
                         [     0.0      , 1.0,       0.0     ],
                         [ torch.sin(ry), 0.0,  torch.cos(ry)]])

    corners = torch.matmul(corners, Ry_T) # rotated corners

    # prepare constrains
    X = torch.eye(4)
    constrains = {}

    # x1, x2, y1 -> 4, 5, 6, 7
    constrains['x1'] = {}
    constrains['x2'] = {}
    constrains['y1'] = {}
    constrains['y2'] = {}

    for i in [4, 5, 6, 7]:
        X[:3, 3] = corners[i]
        K_X = torch.matmul(K, X)

        constrains['x1'][i] = {}
        constrains['x1'][i]['A'] = K_X[0, :3] - x1 * K_X[2, :3]
        constrains['x1'][i]['b'] = x1 * K_X[2, 3] - K_X[0, 3]

        constrains['x2'][i] = {}
        constrains['x2'][i]['A'] = K_X[0, :3] - x2 * K_X[2, :3]
        constrains['x2'][i]['b'] = x2 * K_X[2, 3] - K_X[0, 3]

        constrains['y1'][i] = {}
        constrains['y1'][i]['A'] = K_X[1, :3] - y1 * K_X[2, :3]
        constrains['y1'][i]['b'] = y1 * K_X[2, 3] - K_X[1, 3]

    # y2 -> 0, 1, 2, 3
    for i in [0, 1, 2, 3]:
        X[:3, 3] = corners[i]
        K_X = torch.matmul(K, X)

        constrains['y2'][i] = {}
        constrains['y2'][i]['A'] = K_X[1, :3] - y2 * K_X[2, :3]
        constrains['y2'][i]['b'] = y2 * K_X[2, 3] - K_X[1, 3]

    # solving linear functions
    A = torch.zeros(4, 3)
    b = torch.zeros(4)
    error = float('inf')

    crp = [{'x1': [6, 7], 'x2': [4], 'y1':[5, 7], 'y2': [3]},
           {'x1': [5, 6], 'x2': [7], 'y1':[4, 6], 'y2': [2]},
           {'x1': [4, 5], 'x2': [6], 'y1':[7, 5], 'y2': [1]},
           {'x1': [7, 4], 'x2': [5], 'y1':[6, 4], 'y2': [0]}]

    for i in range(4):
        cr = crp[i]

        for x_1 in cr['x1']:
            A[0] = constrains['x1'][x_1]['A']
            b[0] = constrains['x1'][x_1]['b']
            for x_2 in cr['x2']:
                A[1] = constrains['x2'][x_2]['A']
                b[1] = constrains['x2'][x_2]['b']
                for y_1 in cr['y1']:
                    A[2] = constrains['y1'][y_1]['A']
                    b[2] = constrains['y1'][y_1]['b']
                    for y_2 in cr['y2']:
                        A[3] = constrains['y2'][y_2]['A']
                        b[3] = constrains['y2'][y_2]['b']

                        trans_t = torch.matmul(torch.pinverse(A), b)
                        error_t = torch.norm(torch.matmul(A, trans_t) - b)

                        if error_t < error:
                            trans = trans_t
                            error = error_t

    
    return trans