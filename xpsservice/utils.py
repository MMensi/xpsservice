# -*- coding: utf-8 -*-
import hashlib

import numpy as np
from ase import Atoms
from rdkit import Chem
from rdkit.Chem import AllChem

from .cache import conformer_cache
from .conformers import embed_conformer
from .errors import TooLargeError
from .settings import MAX_ATOMS_FF, MAX_ATOMS_XTB



def hash_object(objec):
    return hashlib.md5(str(objec).encode("utf-8")).hexdigest()


#def get_atoms(molfile:str) -> list:
#    included = list(set([e for e in atoms.symbols if e in list(SOAP.keys())]))
#    atoms = molfile_to_xyz(molfile)
#    excluded = list(set([e for e in atoms.symbols if e not in list(SOAP.keys())]))
#    return included, excluded


def check_max_atoms(mol, max_atoms):
    if mol.GetNumAtoms() > max_atoms:
        raise TooLargeError(
            f"Molecule can have maximal {max_atoms} atoms for this service"
        )

def rdkit2ase(mol):
    pos = mol.GetConformer().GetPositions()
    natoms = mol.GetNumAtoms()
    species = [mol.GetAtomWithIdx(j).GetSymbol() for j in range(natoms)]
    atoms = Atoms(species, positions=pos)
    atoms.pbc = False

    return atoms

def molfile2ase(molfile: str, max_atoms: int = MAX_ATOMS_XTB) -> Atoms:
    try:
        result = conformer_cache.get(molfile)
    except KeyError:
        pass

    if result is None:
        mol = Chem.MolFromMolBlock(molfile, sanitize=True, removeHs=False)
        mol.UpdatePropertyCache(strict=False)
        check_max_atoms(mol, max_atoms)
        mol = embed_conformer(mol)
        result = rdkit2ase(mol), mol
        conformer_cache.set(molfile, result, expire=None)
    return result


def smiles2ase(smiles: str, max_atoms: int = MAX_ATOMS_XTB) -> Atoms:
    try:
        result = conformer_cache.get(smiles)
    except KeyError:
        pass

    if result is None:
        mol = Chem.MolFromSmiles(smiles)
        check_max_atoms(mol, max_atoms)
        refmol = Chem.AddHs(Chem.Mol(mol))
        refmol = embed_conformer(refmol)
        result = rdkit2ase(refmol), refmol
        conformer_cache.set(smiles, result, expire=None)
    return result



def smiles2molfile(smiles: str) -> str:
    # Convert the SMILES string to an RDKit molecule
    mol = Chem.MolFromSmiles(smiles)
    # Check if the molecule was successfully created
    if mol is None:
        raise ValueError("Invalid SMILES string provided.")
    # Add hydrogens, and embed the molecule to generate coordinates
    mol = Chem.AddHs(mol)
    AllChem.EmbedMolecule(mol)
    # Convert the RDKit molecule to a molfile string
    molfile = Chem.MolToMolBlock(mol)
    
    return molfile


def molfile2smiles(molfile: str) -> str:
    """Convert a molfile string to a SMILES string using RDKit."""
    # Parse the molfile using RDKit
    mol = Chem.MolFromMolBlock(molfile)
    if mol is None:
        raise ValueError("Invalid molfile provided.")
    # Convert the molecule to SMILES
    smiles = Chem.MolToSmiles(mol)

    return smiles


def hash_atoms(atoms: Atoms) -> int:
    symbols = str(atoms.symbols)
    positions = str(atoms.positions)

    return hash_object(symbols + positions)

#to remove
def get_center_of_mass(masses, positions):
    return masses @ positions / masses.sum()

#to remove
def get_moments_of_inertia(positions, masses):
    """Get the moments of inertia along the principal axes.

    The three principal moments of inertia are computed from the
    eigenvalues of the symmetric inertial tensor. Periodic boundary
    conditions are ignored. Units of the moments of inertia are
    amu*angstrom**2.
    """
    com = get_center_of_mass(masses, positions)
    positions_ = positions - com  # translate center of mass to origin

    # Initialize elements of the inertial tensor
    I11 = I22 = I33 = I12 = I13 = I23 = 0.0
    for i in range(len(positions_)):
        x, y, z = positions_[i]
        m = masses[i]

        I11 += m * (y ** 2 + z ** 2)
        I22 += m * (x ** 2 + z ** 2)
        I33 += m * (x ** 2 + y ** 2)
        I12 += -m * x * y
        I13 += -m * x * z
        I23 += -m * y * z

    I = np.array([[I11, I12, I13], [I12, I22, I23], [I13, I23, I33]])

    evals, evecs = np.linalg.eigh(I)

    return evals
