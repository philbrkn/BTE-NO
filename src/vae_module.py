# vae_model.py

import torch
from torch import nn
from mpi4py import MPI
import numpy as np

# REMOVE LATER?
image_size = 128
hidden_size = 1024
latent_size = 4

device = torch.device("cpu")  # Change to "cuda" if using GPU


def load_vae_model(rank):
    if rank == 0:
        model = VAE()
        model = torch.load('./model/model', map_location=torch.device('cpu'))
        model.eval()
    else:
        model = None
    model = MPI.COMM_WORLD.bcast(model, root=0)
    return model


class Flatten(nn.Module):
    def forward(self, input):
        return input.view(input.size(0), -1)


class UnFlatten(nn.Module):
    def forward(self, input, size=hidden_size):
        return input.view(input.size(0), size, 1, 1)


class VAE(nn.Module):
    def __init__(self, image_channels=1, h_dim=hidden_size, z_dim=latent_size):
        super(VAE, self).__init__()
        self.encoder = nn.Sequential(
            nn.Conv2d(image_channels, 16, kernel_size=4, stride=2),
            nn.ReLU(),
            nn.Conv2d(16, 32, kernel_size=4, stride=2),
            nn.ReLU(),
            nn.Conv2d(32, 64, kernel_size=4, stride=2),
            nn.ReLU(),
            nn.Conv2d(64, 128, kernel_size=4, stride=2),
            nn.ReLU(),
            nn.Conv2d(128, 256, kernel_size=4, stride=2),
            nn.ReLU(),
            Flatten(),
        )

        self.fc1 = nn.Linear(h_dim, z_dim)
        self.fc2 = nn.Linear(h_dim, z_dim)
        self.fc3 = nn.Linear(z_dim, h_dim)

        self.decoder = nn.Sequential(
            UnFlatten(),
            nn.ConvTranspose2d(h_dim, 128, kernel_size=5, stride=2),
            nn.ReLU(),
            nn.ConvTranspose2d(128, 64, kernel_size=5, stride=2),
            nn.ReLU(),
            nn.ConvTranspose2d(64, 32, kernel_size=5, stride=2),
            nn.ReLU(),
            nn.ConvTranspose2d(32, 16, kernel_size=6, stride=2),
            nn.ReLU(),
            nn.ConvTranspose2d(16, image_channels, kernel_size=6, stride=2),
            nn.Sigmoid(),
        )

    def reparameterize(self, mu, logvar):
        std = logvar.mul(0.5).exp_()
        # eps = torch.randn_like(std)
        esp = torch.randn(*mu.size()).to(device)
        z = mu + std * esp
        return z

    def bottleneck(self, h):
        mu, logvar = self.fc1(h), self.fc2(h)
        z = self.reparameterize(mu, logvar)
        return z, mu, logvar

    def encode(self, x):
        h = self.encoder(x)
        z, mu, logvar = self.bottleneck(h)
        return z, mu, logvar

    def decode(self, z):
        z = self.fc3(z)
        z = self.decoder(z)
        return z

    def forward(self, x):
        z, mu, logvar = self.encode(x)
        z = self.decode(z)
        return z, mu, logvar


def apply_volume_fraction(img, vol_fraction):
    # Flatten the image to a 1D array
    img_flat = img.flatten()
    # Sort the pixels
    sorted_pixels = np.sort(img_flat)
    # Determine the threshold that will result in the desired volume fraction
    num_pixels = img_flat.size
    k = int((1 - vol_fraction) * num_pixels)
    if k == 0:
        threshold = sorted_pixels[0] - 1e-5  # All pixels are solid
    elif k == num_pixels:
        threshold = sorted_pixels[-1] + 1e-5  # No pixels are solid
    else:
        threshold = (sorted_pixels[k] + sorted_pixels[k - 1]) / 2.0
    # Apply the threshold
    img_binary = (img >= threshold).astype(np.float32)
    return img_binary


def z_to_img(z, model, vol_fraction, device=device):
    z = torch.from_numpy(z).float().unsqueeze(0).to(device)  # Add batch dimension
    model.eval()
    with torch.no_grad():
        sample = model.decode(z)
        # Remove batch and channel dimensions
        img = sample.squeeze().squeeze().cpu().numpy()
        # Flip the image
        img = img[::-1, :]
        # Take the left half of the image and resymmetrize
        img = img[:, :img.shape[1] // 2]
        img = np.concatenate((img, img[:, ::-1]), axis=1)
    # Apply volume fraction control
    img_binary = apply_volume_fraction(img, vol_fraction)
    return img_binary
