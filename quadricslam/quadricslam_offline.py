"""
QuadricSLAM Copyright 2020, ARC Centre of Excellence for Robotic Vision, Queensland University of Technology (QUT)
Brisbane, QLD 4000
All Rights Reserved

See LICENSE for the license information

Description: System (front-end) interface 
Author: Lachlan Nicholson (Python)
"""

# import standard libraries
import os
import sys
import numpy as np
import matplotlib.pyplot as plt
import configparser
import argparse
import yaml

# import gtsam and extension
import gtsam
import gtsam_quadrics

# import custom python modules
sys.dont_write_bytecode = True
from dataset_interfaces.simulated_dataset import SimulatedSequence
from dataset_interfaces.scenenet_dataset import SceneNetDataset
from visualization.drawing import MPLDrawing
from base.evaluation import Evaluation
from base.containers import *


class QuadricSLAM_Offline(object):
    """
    Python front-end to build graph/estimate from dataset. 
    """
    X = lambda i: int(gtsam.symbol(ord('x'), i))
    Q = lambda i: int(gtsam.symbol(ord('q'), i))

    @staticmethod
    def run(sequence, config):

        # build graph / estimate
        graph, initial_estimate = QuadricSLAM_Offline.build_graph(sequence, config)

        # draw initial system
        plotting = MPLDrawing('initial_problem')
        plotting.plot_system(graph, initial_estimate)

        # optimize using c++ back-end
        estimate = QuadricSLAM_Offline.optimize(graph, initial_estimate)

        # draw estimation
        plotting = MPLDrawing('final_solution')
        plotting.plot_system(graph, estimate)

        # extract quadrics / trajectory 
        estimated_trajectory = Trajectory.from_values(estimate)
        estimated_quadrics = Quadrics.from_values(estimate)

        # evaluate results
        initial_ATE_H = Evaluation.evaluate_trajectory(Trajectory.from_values(initial_estimate), sequence.true_trajectory, horn=True)[0]
        estimate_ATE_H = Evaluation.evaluate_trajectory(estimated_trajectory, sequence.true_trajectory, horn=True)[0]
        initial_ATE = Evaluation.evaluate_trajectory(Trajectory.from_values(initial_estimate), sequence.true_trajectory, horn=False)[0]
        estimate_ATE = Evaluation.evaluate_trajectory(estimated_trajectory, sequence.true_trajectory, horn=False)[0]
        print('Initial ATE w/ horn alignment: {}'.format(initial_ATE_H))
        print('Final ATE w/ horn alignment:   {}'.format(estimate_ATE_H))
        print('Initial ATE w/ weak alignment: {}'.format(initial_ATE))
        print('Final ATE w/ weak alignment:   {}'.format(estimate_ATE))

        # plot results
        trajectories = [Trajectory.from_values(initial_estimate), estimated_trajectory, sequence.true_trajectory]
        maps = [Quadrics.from_values(initial_estimate), estimated_quadrics]
        colors = ['r', 'm', 'g']; names = ['initial_estimate', 'final_estimate', 'ground_truth']
        plotting.plot_result(trajectories, maps, colors, names)
           
    
    @staticmethod
    def optimize(graph, initial_estimate):

        # create optimizer parameters
        params = gtsam.LevenbergMarquardtParams()
        params.setVerbosityLM("SUMMARY")    # SILENT = 0, SUMMARY, TERMINATION, LAMBDA, TRYLAMBDA, TRYCONFIG, DAMPED, TRYDELTA : VALUES, ERROR 
        params.setMaxIterations(100)
        params.setlambdaInitial(1e-5)
        params.setlambdaUpperBound(1e10)
        params.setlambdaLowerBound(1e-8)
        params.setRelativeErrorTol(1e-5)
        params.setAbsoluteErrorTol(1e-5)
  
        # create optimizer
        optimizer = gtsam.LevenbergMarquardtOptimizer(graph, initial_estimate, params)

        # run optimizer
        print('starting optimization')
        estimate = optimizer.optimize()
        print('optimization finished')

        return estimate


    @staticmethod
    def build_graph(sequence, config):
        """
        Adds noise to sequence variables / measurements. 
        Returns graph, initial_estimate
        """

        # create empty graph / estimate
        graph = gtsam.NonlinearFactorGraph()
        initial_estimate = gtsam.Values()

        # load camera calibration
        calibration = QuadricSLAM_Offline.load_calibration(config)

        # declare noise models
        prior_noise = gtsam.noiseModel_Diagonal.Sigmas(np.array([config['QuadricSLAM.prior_sd']]*6, dtype=np.float))
        odometry_noise = gtsam.noiseModel_Diagonal.Sigmas(np.array([config['QuadricSLAM.odom_sd']]*6, dtype=np.float))
        bbox_noise = gtsam.noiseModel_Diagonal.Sigmas(np.array([config['QuadricSLAM.box_sd']]*4, dtype=np.float))

        # get noisy odometry / detections 
        true_odometry = sequence.true_trajectory.as_odometry()
        noisy_odometry = true_odometry.add_noise(mu=0.0, sd=config['Noise.odom_sd'])
        noisy_detections = sequence.true_detections.add_noise(mu=0.0, sd=config['Noise.box_sd'])

        # initialize trajectory
        # TODO: ensure aligned in same reference frame
        initial_trajectory = noisy_odometry.as_trajectory(sequence.true_trajectory.values()[0])

        # initialize quadrics
        initial_quadrics = QuadricSLAM_Offline.initialize_quadrics(initial_trajectory, noisy_detections, calibration)

        # add prior pose
        prior_factor = gtsam.PriorFactorPose3(QuadricSLAM_Offline.X(0), initial_trajectory.at(0), prior_noise)
        graph.add(prior_factor)

        # add odometry measurements
        for (start_key, end_key), rpose in noisy_odometry.items():
            odometry_factor = gtsam.BetweenFactorPose3(QuadricSLAM_Offline.X(start_key), QuadricSLAM_Offline.X(end_key), rpose, odometry_noise)
            graph.add(odometry_factor)

        # add initial pose estimates
        for pose_key, pose in initial_trajectory.items():
            initial_estimate.insert(QuadricSLAM_Offline.X(pose_key), pose)

        # add valid box measurements
        valid_objects = []
        initialized_quadrics = initial_quadrics.keys()
        for object_key, object_detections in noisy_detections.per_object():

            # add if quadric initialized
            if object_key in initialized_quadrics:
                
                # add if enough views
                if len(object_detections) > 3:

                    # add measurements
                    valid_objects.append(object_key)
                    for pose_key, detection in object_detections.items():
                        bbf = gtsam_quadrics.BoundingBoxFactor(detection.box, calibration, QuadricSLAM_Offline.X(pose_key), QuadricSLAM_Offline.Q(object_key), bbox_noise)
                        graph.add(bbf)

        # add initial landmark estimates
        for object_key, quadric in initial_quadrics.items():

            # add if seen > 3 times
            if (object_key in valid_objects):
                quadric.addToValues(initial_estimate, QuadricSLAM_Offline.Q(object_key))

        return graph, initial_estimate

    @staticmethod
    def load_calibration(config):
        camera_model = gtsam.Cal3_S2
        calibration_list = [
            config['Camera.fx'],
            config['Camera.fy'],
            0.0,
            config['Camera.cx'],
            config['Camera.cy'],
        ]
        return camera_model(*calibration_list)

    @staticmethod
    def initialize_quadrics(trajectory, detections, calibration):
        """ Uses SVD to initialize quadrics from measurements """
        quadrics = Quadrics()

        # loop through object keys
        for object_key, object_detections in detections.per_object():

            # get the object boxes from each pose
            object_boxes = [d.box for d in object_detections.values()]

            # get the poses associated with each detection 
            pose_keys = list(object_detections.keys())
            poses = trajectory.at_keys(pose_keys)

            # ensure quadric seen from > 3 views
            if len(np.unique(pose_keys)) < 3:
                continue

            # initialize quadric fomr views using svd
            quadric_matrix = QuadricSLAM_Offline.quadric_SVD(poses, object_boxes, calibration)

            # constrain generic dual quadric to be ellipsoidal 
            quadric = gtsam_quadrics.ConstrainedDualQuadric.constrain(quadric_matrix)

            # check quadric is okay
            if (QuadricSLAM_Offline.is_okay(quadric, poses, calibration)):
                quadrics.add(quadric, object_key)

        return quadrics

    @staticmethod
    def quadric_SVD(poses, object_boxes, calibration):
        """ calculates quadric_matrix using SVD """

        # iterate through box/pose data
        planes = []
        for box, pose in zip(object_boxes, poses):

            # calculate boxes lines
            lines = box.lines()

            # convert Vector3Vector to list
            lines = [lines.at(i) for i in range(lines.size())]

            # calculate projection matrix
            P = gtsam_quadrics.QuadricCamera.transformToImage(pose, calibration).transpose()

            # project lines to planes
            planes += [P @ line for line in lines]

        # create A matrix
        A = np.asarray([np.array([p[0]**2,  2*(p[0]*p[1]),  2*(p[0]*p[2]),  2*(p[0]*p[3]),
                                                p[1]**2,  	2*(p[1]*p[2]),  2*(p[1]*p[3]),
                                                                p[2]**2,  	2*(p[2]*p[3]),
                                                                               p[3]**2]) for p in planes])

        # solve SVD for Aq = 0, which should be equal to p'Qp = 0
        _,_,V = np.linalg.svd(A, full_matrices=True)
        q = V.T[:, -1]

        # construct quadric
        dual_quadric = np.array([[q[0], q[1], q[2], q[3]],
                                [q[1], q[4], q[5], q[6]],
                                [q[2], q[5], q[7], q[8]],
                                [q[3], q[6], q[8], q[9]]])

        return dual_quadric

    @staticmethod
    def is_okay(quadric, poses, calibration):
        """
        Checks quadric is valid:
            quadric constrained correctly
            paralax > threshold
            reprojections valid in each frame 
                quadric infront of camera : positive depth 
                camera outside quadric
                conic is an ellipse 
            ensure views provide enough DOF (due to edges / out of frame)
        """
        for pose in poses:

            # quadric must have positive depth
            if quadric.isBehind(pose):
                return False

            # camera pose must be outside quadric 
            if quadric.contains(pose):
                return False

            # conic must be valid and elliptical 
            conic = gtsam_quadrics.QuadricCamera.project(quadric, pose, calibration)
            if conic.isDegenerate():
                return False
            if not conic.isEllipse():
                return False
                
        return True






if __name__ == '__main__':
    # parse input arguments for config file
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', help='path to config file', required=True)
    args = parser.parse_args()

    # load system configuration
    config = yaml.safe_load(open(args.config, 'r'))

    # validate configuration 
    if config['Dataset.name'] == 'SceneNet':

        # if SceneNet, ensure all paths are provided 
        required_settings = ['Dataset.data', 'Dataset.protobufs', 'Dataset.protobuf_definition', 'Dataset.sequence_n']
        if not all(setting in config for setting in required_settings):
            raise RuntimeError("If SceneNet selected as dataset, must provide path to SceneNet files.")

    # set experiment seed 
    np.random.seed(config['Noise.seed'])

    # load dataset
    if config['Dataset.name'] == 'SceneNet':
        dataset = SceneNetDataset(
            dataset_path = config['Dataset.data'],
            protobuf_folder = config['Dataset.protobufs'],
            reader_path = config['Dataset.protobuf_definition'],
        )
        sequence = dataset[config['Dataset.sequence_n']]
    else:
        sequence = SimulatedSequence.sequence1()

    # run system on sequence 
    QuadricSLAM_Offline.run(sequence, config)

