# SPDX-FileCopyrightText: Copyright (c) 2023 - 2024 NVIDIA CORPORATION & AFFILIATES.
# SPDX-FileCopyrightText: All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import warnings

from sympy import Symbol, Eq, And
import torch

import physicsnemo.sym
from physicsnemo.sym.hydra import to_absolute_path, instantiate_arch, PhysicsNeMoConfig
from physicsnemo.sym.utils.io import csv_to_dict
from physicsnemo.sym.solver import Solver
from physicsnemo.sym.domain import Domain
from physicsnemo.sym.geometry.primitives_2d import Rectangle, Circle
from physicsnemo.sym.utils.sympy.functions import parabola
from physicsnemo.sym.domain.constraint import (
    PointwiseBoundaryConstraint,
    PointwiseInteriorConstraint,
    IntegralBoundaryConstraint,
)

from physicsnemo.sym.domain.validator import PointwiseValidator
from physicsnemo.sym.domain.inferencer import PointwiseInferencer
from physicsnemo.sym.domain.monitor import PointwiseMonitor
from physicsnemo.sym.key import Key
from physicsnemo.sym.node import Node
from physicsnemo.sym.eq.pdes.navier_stokes import NavierStokes
from physicsnemo.sym.eq.pdes.basic import NormalDotVec


@physicsnemo.sym.main(config_path="conf", config_name="config")
def run(cfg: PhysicsNeMoConfig) -> None:
    # make list of nodes to unroll graph on
    ns = NavierStokes(nu=0.01, rho=1.0, dim=2, time=False)
    normal_dot_vel = NormalDotVec(["u", "v"])
    flow_net = instantiate_arch(
        input_keys=[Key("x"), Key("y")],
        output_keys=[Key("u"), Key("v"), Key("p")],
        cfg=cfg.arch.fully_connected,
    )
    nodes = (
        ns.make_nodes(
            create_instances=2,
            freeze_terms={
                "continuity_0": [],
                "momentum_x_0": [1, 2],
                "momentum_y_0": [1, 2],
                "continuity_1": [],
                "momentum_x_1": [3, 4],
                "momentum_y_1": [3, 4],
            },
        )
        + normal_dot_vel.make_nodes()
        + [flow_net.make_node(name="flow_network", jit=cfg.jit)]
    )

    # add constraints to solver
    # specify params
    channel_length = (-6.732, 6.732)
    channel_width = (-1.0, 1.0)
    cylinder_center = (0.0, 0.0)
    outer_cylinder_radius = 2.0
    inner_cylinder_radius = 1.0
    inlet_vel = 1.5

    # make geometry
    x, y = Symbol("x"), Symbol("y")
    rec = Rectangle(
        (channel_length[0], channel_width[0]), (channel_length[1], channel_width[1])
    )
    outer_circle = Circle(cylinder_center, outer_cylinder_radius)
    inner_circle = Circle((0, 0), inner_cylinder_radius)
    geo = (rec + outer_circle) - inner_circle

    # make annular ring domain
    domain = Domain()

    # inlet
    inlet_sympy = parabola(
        y, inter_1=channel_width[0], inter_2=channel_width[1], height=inlet_vel
    )
    inlet = PointwiseBoundaryConstraint(
        nodes=nodes,
        geometry=geo,
        outvar={"u": inlet_sympy, "v": 0},
        batch_size=cfg.batch_size.inlet,
        criteria=Eq(x, channel_length[0]),
    )
    domain.add_constraint(inlet, "inlet")

    # outlet
    outlet = PointwiseBoundaryConstraint(
        nodes=nodes,
        geometry=geo,
        outvar={"p": 0},
        batch_size=cfg.batch_size.outlet,
        criteria=Eq(x, channel_length[1]),
    )
    domain.add_constraint(outlet, "outlet")

    # no slip
    no_slip = PointwiseBoundaryConstraint(
        nodes=nodes,
        geometry=geo,
        outvar={"u": 0, "v": 0},
        batch_size=cfg.batch_size.no_slip,
        criteria=And((x > channel_length[0]), (x < channel_length[1])),
    )
    domain.add_constraint(no_slip, "no_slip")

    # interior
    interior = PointwiseInteriorConstraint(
        nodes=nodes,
        geometry=geo,
        outvar={
            "continuity_0": 0,
            "momentum_x_0": 0,
            "momentum_y_0": 0,
            "continuity_1": 0,
            "momentum_x_1": 0,
            "momentum_y_1": 0,
        },
        batch_size=cfg.batch_size.interior,
        lambda_weighting={
            "continuity_0": Symbol("sdf"),
            "momentum_x_0": Symbol("sdf"),
            "momentum_y_0": Symbol("sdf"),
            "continuity_1": Symbol("sdf"),
            "momentum_x_1": Symbol("sdf"),
            "momentum_y_1": Symbol("sdf"),
        },
    )
    domain.add_constraint(interior, "interior")

    # integral continuity
    integral_continuity = IntegralBoundaryConstraint(
        nodes=nodes,
        geometry=geo,
        outvar={"normal_dot_vel": 2},
        batch_size=1,
        integral_batch_size=cfg.batch_size.integral_continuity,
        lambda_weighting={"normal_dot_vel": 0.1},
        criteria=Eq(x, channel_length[1]),
    )
    domain.add_constraint(integral_continuity, "integral_continuity")

    # add validation data
    file_path = "../openfoam/bend_finerInternal0.csv"
    if os.path.exists(to_absolute_path(file_path)):
        mapping = {"Points:0": "x", "Points:1": "y", "U:0": "u", "U:1": "v", "p": "p"}
        openfoam_var = csv_to_dict(to_absolute_path(file_path), mapping)
        openfoam_var["x"] += channel_length[0]  # center OpenFoam data
        openfoam_invar_numpy = {
            key: value for key, value in openfoam_var.items() if key in ["x", "y"]
        }
        openfoam_outvar_numpy = {
            key: value for key, value in openfoam_var.items() if key in ["u", "v", "p"]
        }
        openfoam_validator = PointwiseValidator(
            nodes=nodes,
            invar=openfoam_invar_numpy,
            true_outvar=openfoam_outvar_numpy,
            batch_size=1024,
        )
        domain.add_validator(openfoam_validator)

        # add inferencer data
        grid_inference = PointwiseInferencer(
            nodes=nodes,
            invar=openfoam_invar_numpy,
            output_names=["u", "v", "p"],
            batch_size=512,
        )
        domain.add_inferencer(grid_inference, "inf_data")
    else:
        warnings.warn(
            f"Directory {file_path} does not exist. Will skip adding validators. Please download the additional files from NGC https://catalog.ngc.nvidia.com/orgs/nvidia/teams/physicsnemo/resources/Modulus_sym_examples_supplemental_materials"
        )

    # add monitors
    # metric for mass and momentum imbalance
    global_monitor = PointwiseMonitor(
        geo.sample_interior(1024),
        output_names=["continuity_0", "momentum_x_0", "momentum_y_0"],
        metrics={
            "mass_imbalance_0": lambda var: torch.sum(
                var["area"] * torch.abs(var["continuity_0"])
            ),
            "momentum_imbalance_0": lambda var: torch.sum(
                var["area"]
                * (torch.abs(var["momentum_x_0"]) + torch.abs(var["momentum_y_0"]))
            ),
        },
        nodes=nodes,
        requires_grad=True,
    )
    domain.add_monitor(global_monitor)

    global_monitor = PointwiseMonitor(
        geo.sample_interior(1024),
        output_names=["continuity_1", "momentum_x_1", "momentum_y_1"],
        metrics={
            "mass_imbalance_1": lambda var: torch.sum(
                var["area"] * torch.abs(var["continuity_1"])
            ),
            "momentum_imbalance_1": lambda var: torch.sum(
                var["area"]
                * (torch.abs(var["momentum_x_1"]) + torch.abs(var["momentum_y_1"]))
            ),
        },
        nodes=nodes,
        requires_grad=True,
    )
    domain.add_monitor(global_monitor)

    # metric for force on inner sphere
    force_monitor = PointwiseMonitor(
        inner_circle.sample_boundary(1024),
        output_names=["p"],
        metrics={
            "force_x": lambda var: torch.sum(var["normal_x"] * var["area"] * var["p"]),
            "force_y": lambda var: torch.sum(var["normal_y"] * var["area"] * var["p"]),
        },
        nodes=nodes,
    )
    domain.add_monitor(force_monitor)

    # make solver
    slv = Solver(cfg, domain)

    # start solver
    slv.solve()


if __name__ == "__main__":
    run()
