# main.py

# import libraries
import torch
from mpi4py import MPI
import argparse
import numpy as np
from petsc4py import PETSc

# import modules
# from logging_module import LoggingModule
from mesh_generator import MeshGenerator
from vae_module import load_vae_model, VAE, Flatten, UnFlatten
from image_processing import z_to_img
from optimization_module import BayesianModule, CMAESModule
from post_processing import PostProcessingModule
from solver_module import Solver


class SimulationConfig:
    def __init__(self, args):
        # Physical properties
        self.C = PETSc.ScalarType(1.0)  # Slip parameter for fully diffusive boundaries
        self.T_ISO = PETSc.ScalarType(0.0)  # Isothermal temperature, K
        self.Q_L = PETSc.ScalarType(80)  # Line heat source, W/m

        self.MEAN_FREE_PATH = 0.439e-6  # Characteristic length, adjust as necessary
        self.KNUDSEN = 1  # Knudsen number, adjust as necessary

        # Volume fraction
        if args.vf < 0:
            self.vol_fraction = None
        elif args.vf:
            self.vol_fraction = args.vf
        else:
            self.vol_fraction = 0.2  # Default to 20%

        # Geometric properties
        self.LENGTH = self.MEAN_FREE_PATH / self.KNUDSEN  # Characteristic length, L
        # Base rectangle:
        self.L_X = 25 * self.LENGTH
        self.L_Y = 12.5 * self.LENGTH
        # Source rectangle:
        self.SOURCE_WIDTH = self.LENGTH
        self.SOURCE_HEIGHT = self.LENGTH * 0.25
        self.mask_extrusion = True

        # Set the resolution of the mesh as a divider of LENGTH
        if args.res is not None and args.res > 0:
            self.RESOLUTION = self.LENGTH / args.res
        else:
            self.RESOLUTION = self.LENGTH / 12  # default value
            # self.RESOLUTION = self.LENGTH / 5  # to get quick profiles

        # material properties
        self.ELL_SI = PETSc.ScalarType(
            self.MEAN_FREE_PATH / np.sqrt(5)
        )  # Non-local length, m
        self.ELL_DI = PETSc.ScalarType(196e-8)
        self.KAPPA_SI = PETSc.ScalarType(141.0)  # W/mK, thermal conductivity
        self.KAPPA_DI = PETSc.ScalarType(600.0)

        if args.sources is not None:
            if len(args.sources) % 2 != 0:
                raise ValueError(
                    "Each source must have a position and a heat value. Please provide pairs of values."
                )
            # Group the list into pairs
            sources_pairs = [
                (args.sources[i], args.sources[i + 1]) for i in range(0, len(args.sources), 2)
            ]
            self.source_positions = []
            self.Q_sources = []
            for pos, Q in sources_pairs:
                if pos < 0 or pos > 1:
                    raise ValueError("Source positions must be between 0 and 1 (normalized).")
                self.source_positions.append(pos)
                self.Q_sources.append(PETSc.ScalarType(Q))
            # Sort the sources by position
            combined_sources = sorted(zip(self.source_positions, self.Q_sources))
            self.source_positions, self.Q_sources = list(zip(*combined_sources))
        else:
            # Default to a single source at the center
            self.source_positions = [0.5]
            self.Q_sources = [PETSc.ScalarType(self.Q_L)]

        # Cannot be symmetry and two sources:
        if len(self.source_positions) > 1 and args.symmetry:
            raise ValueError("Cannot have both symmetry and two sources.")

        # symmetry in geometry
        if args.symmetry:
            self.L_X = self.L_X / 2
            self.SOURCE_WIDTH = self.SOURCE_WIDTH / 2
            self.symmetry = True  # Enable or disable symmetry
        else:
            self.symmetry = False

        if args.blank:
            self.mask_extrusion = False

        # Parse visualize argument
        self.visualize = args.visualize
        if "all" in self.visualize and len(self.visualize) > 1:
            self.visualize.remove("all")
        if "none" in self.visualize and len(self.visualize) > 1:
            raise ValueError("Cannot combine 'none' with other visualization options.")

        self.optim = args.optim
        self.optimizer = args.optimizer
        self.latent = args.latent
        self.blank = args.blank


def main(config):
    # Load VAE model
    comm = MPI.COMM_WORLD
    rank = MPI.COMM_WORLD.rank
    model = load_vae_model(rank)

    # Mesh generation
    mesh_generator = MeshGenerator(config)
    if config.symmetry:
        msh, cell_markers, facet_markers = mesh_generator.sym_create_mesh()
    else:
        msh, cell_markers, facet_markers = mesh_generator.create_mesh()

    time1 = MPI.Wtime()

    # Create solver instance
    solver = Solver(msh, facet_markers, config)

    if config.optim:
        # Run optimization
        if config.optimizer == "cmaes":
            optimizer = CMAESModule(solver, model, torch.device("cpu"), rank, config)
            best_z_list = optimizer.optimize(n_iter=100)  # Adjust iterations as needed
        elif config.optimizer == "bayesian":
            optimizer = BayesianModule(solver, model, torch.device("cpu"), rank, config)
            best_z_list = optimizer.optimize(init_points=10, n_iter=100)

        latent_vectors = best_z_list
        # Optional: Save the best_z to a file for future solving
        if rank == 0:
            np.save("best_latent_vector.npy", best_z_list)

    # Run solving based on provided latent vector
    else:
        if rank == 0:
            # Load best latent vectors from file if available
            try:
                best_z_list = np.load("best_latent_vector.npy", allow_pickle=True)
                print("Opening best vector")
                # print shape:
                # Ensure it's a list of arrays
                latent_vectors = best_z_list
            except FileNotFoundError:
                # If no file exists, use default latent vectors
                # One latent vector per source
                latent_vectors = [np.array([0.8019, 1.0, -0.5918, 0.4979])] * len(config.source_positions)
                print("No saved latent vectors found. Using default latent vectors.")
        else:
            latent_vectors = None
        # Broadcast the latent_vectors list to all ranks
        latent_vectors = comm.bcast(latent_vectors, root=0)

    # Generate image from latent vector
    if rank == 0:
        img_list = []
        for z in latent_vectors:
            if config.blank:
                img = np.zeros((128, 128))
            else:
                # Ensure z is reshaped correctly if needed
                img = z_to_img(z.reshape(1, -1), model, config.vol_fraction)
            img_list.append(img)

        # Apply symmetry to each image if enabled
        if config.symmetry:
            img_list = [img[:, : img.shape[1] // 2] for img in img_list]
    else:
        img_list = None

    img_list = MPI.COMM_WORLD.bcast(img_list, root=0)

    if "pregamma" in config.visualize:
        plot_image_list(img_list)

    # Solve the image using the solver
    avg_temp_global = solver.solve_image(img_list)
    time2 = MPI.Wtime()
    if rank == 0:
        print(f"Average temperature: {avg_temp_global} K")
        print(f"Time taken to solve: {time2 - time1:.3f} seconds")

    # Optional Post-processing
    if "none" not in config.visualize:
        post_processor = PostProcessingModule(rank, config)
        post_processor.postprocess_results(solver.U, solver.msh, solver.gamma)


def parse_arguments():
    # Command-line arguments to determine the modes
    parser = argparse.ArgumentParser()
    parser.add_argument("--optim", action="store_true", help="Run optimization.")
    parser.add_argument(
        "--optimizer",
        choices=["cmaes", "bayesian"],
        default="bayesian",
        help="Choose between 'cmaes' or 'bayesian' optimization (default: bayesian)."
    )
    parser.add_argument(
        "--latent",
        nargs=4,
        type=float,
        default=None,
        help="Specify z values (z1, z2, z3, z4) for 'solve' mode.",
    )
    parser.add_argument(
        "--symmetry",
        action="store_true",
        help="Enable left-right symmetry in the domain.",
    )
    parser.add_argument("--blank", action="store_true", help="Run with a blank image.")
    parser.add_argument(
        "--sources",
        nargs="*",
        type=float,
        default=None,
        help="List of source positions and heat source values as pairs, e.g., --sources 0.5 80 0.75 40",
    )
    parser.add_argument(
        "--res",
        type=float,
        default=None,
        help="Set the mesh resolution (default: LENGTH / 12).",
    )
    parser.add_argument(
        "--visualize",
        nargs="*",
        type=str,
        default=["all"],
        choices=["none", "gamma", "temperature", "flux", "all", "pregamma"],
        help=(
            "Specify what to visualize. Options: "
            "'none' (no visualization), 'gamma', 'temperature', 'flux', 'profiles', 'all' "
            "(default: all). Multiple options can be specified."
        ),
    )
    parser.add_argument(
        "--vf",
        type=float,
        default=0.2,
        help=("Set the desired volume fraction (default: 0.2)."
              "Negative means no volume fraction control."),
    )

    return parser.parse_args()


# plot image list function
def plot_image_list(img_list):
    import matplotlib.pyplot as plt

    fig, axs = plt.subplots(1, len(img_list), figsize=(15, 5))
    for i, img in enumerate(img_list):
        axs[i].imshow(img, cmap="gray")
        axs[i].axis("off")
    # save to a file
    plt.savefig("image_list.png")


if __name__ == "__main__":
    # Parse command-line arguments
    args = parse_arguments()

    # Initialize configuration
    config = SimulationConfig(args)

    main(config)
