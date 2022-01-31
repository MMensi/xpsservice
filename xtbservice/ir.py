# -*- coding: utf-8 -*-
import shutil
from functools import lru_cache
from typing import List, Tuple, Union

import numpy as np
import wrapt_timeout_decorator
from ase import Atoms
from ase.calculators.bond_polarizability import BondPolarizability
from ase.vibrations import Infrared
from ase.vibrations.placzek import PlaczekStatic
from ase.vibrations.raman import StaticRamanCalculator
from fastapi.logger import logger
from rdkit import Chem
from scipy import spatial
from xtb.ase.calculator import XTB

from .cache import ir_cache, ir_from_molfile_cache, ir_from_smiles_cache
from .models import IRResult
from .optimize import run_xtb_opt
from .settings import IMAGINARY_FREQ_THRESHOLD, MAX_ATOMS_FF, MAX_ATOMS_XTB, TIMEOUT
from .utils import (
    get_moments_of_inertia,
    hash_atoms,
    hash_object,
    molfile2ase,
    smiles2ase,
)


def get_max_atoms(method):
    if method == "GFNFF":
        return MAX_ATOMS_FF
    elif method == "GFN2xTB":
        return MAX_ATOMS_XTB
    elif method == "GFN1xTB":
        return MAX_ATOMS_XTB


def ir_hash(atoms, method):
    return hash_object(str(hash_atoms(atoms)) + method)


def get_raman_spectrum(
    pz,
    start=0,
    end=4000,
    npts=None,
    width=4,
    type="Gaussian",
    method="standard",
    direction="central",
    intensity_unit="(D/A)2/amu",
    normalize=False,
):
    """Get infrared spectrum.

    The method returns wavenumbers in cm^-1 with corresponding
    absolute infrared intensity.
    Start and end point, and width of the Gaussian/Lorentzian should
    be given in cm^-1.
    normalize=True ensures the integral over the peaks to give the
    intensity.
    """
    frequencies = pz.vibrations.get_frequencies(method, direction).real
    intensities = pz.get_absolute_intensities()
    return pz.vibrations.fold(
        frequencies, intensities, start, end, npts, width, type, normalize
    )


def run_xtb_ir(
    atoms: Atoms, method: str = "GFNFF", mol: Union[None, Chem.Mol] = None
) -> IRResult:
    if mol is None:
        raise Exception

    this_hash = ir_hash(atoms, method)
    logger.debug(f"Running IR for {this_hash}")
    moi = atoms.get_moments_of_inertia()
    linear = sum(moi > 0.01) == 2
    result = ir_cache.get(this_hash)

    if result is None:
        logger.debug(f"IR not in cache for {this_hash}, running")
        atoms.pbc = False
        atoms.calc = XTB(method=method)

        try:
            rm = StaticRamanCalculator(atoms, BondPolarizability, name=str(this_hash))
            rm.ir = True
            rm.run()
            pz = PlaczekStatic(atoms, name=str(this_hash))
            raman_intensities = pz.get_absolute_intensities()
            raman_spectrum = list(get_raman_spectrum(pz)[1])

        except Exception as e:
            shutil.rmtree(str(this_hash))
            raman_intensities = None
            raman_spectrum = None

        ir = Infrared(atoms, name=str(this_hash))
        ir.run()

        spectrum = ir.get_spectrum(start=0, end=4000)

        if raman_spectrum is not None:
            assert len(spectrum[0]) == len(raman_spectrum)
        zpe = ir.get_zero_point_energy()
        most_relevant_mode_for_bond = None
        bond_displacements = None
        if mol is not None:
            bond_displacements = compile_all_bond_displacements(mol, atoms, ir)
            mask = np.zeros_like(bond_displacements)
            if len(atoms) > 2:
                if linear:
                    mask[:5, :] = 1
                else:
                    mask[:6, :] = 1
                masked_bond_displacements = np.ma.masked_array(bond_displacements, mask)
            else:
                masked_bond_displacements = bond_displacements
            most_relevant_mode_for_bond_ = masked_bond_displacements.argmax(axis=0)
            most_relevant_mode_for_bond = []
            bonds = get_bonds_from_mol(mol)
            for i, mode in enumerate(most_relevant_mode_for_bond_):
                most_relevant_mode_for_bond.append(
                    {
                        "startAtom": bonds[i][0],
                        "endAtom": bonds[i][1],
                        "mode": int(mode),
                        "displacement": bond_displacements[mode][i],
                    }
                )
        displacement_alignments = [
            get_alignment(ir, n) for n in range(3 * len(ir.indices))
        ]

        mode_info, has_imaginary, has_large_imaginary = compile_modes_info(
            ir,
            linear,
            displacement_alignments,
            bond_displacements,
            bonds,
            raman_intensities,
        )
        result = IRResult(
            wavenumbers=list(spectrum[0]),
            intensities=list(spectrum[1]),
            ramanIntensities=raman_spectrum,
            zeroPointEnergy=zpe,
            modes=mode_info,
            hasImaginaryFrequency=has_imaginary,
            mostRelevantModesOfAtoms=get_max_displacements(ir, linear),
            mostRelevantModesOfBonds=most_relevant_mode_for_bond,
            isLinear=linear,
            momentsOfInertia=[float(i) for i in moi],
            hasLargeImaginaryFrequency=has_large_imaginary,
        )
        ir_cache.set(this_hash, result)

        shutil.rmtree(ir.cache.directory)
        ir.clean()
    return result


#@wrapt_timeout_decorator.timeout(TIMEOUT, use_signals=False)
def calculate_from_smiles(smiles, method, myhash):
    atoms, mol = smiles2ase(smiles, get_max_atoms(method))
    opt_result = run_xtb_opt(atoms, method=method)
    result = run_xtb_ir(opt_result.atoms, method=method, mol=mol)
    ir_from_smiles_cache.set(myhash, result, expire=None)
    return result


def ir_from_smiles(smiles, method):
    myhash = hash_object(smiles + method)
    result = ir_from_smiles_cache.get(myhash)
    if result is None:
        result = calculate_from_smiles(smiles, method, myhash)
    return result


#@wrapt_timeout_decorator.timeout(TIMEOUT, use_signals=False)
def calculate_from_molfile(molfile, method, myhash):
    atoms, mol = molfile2ase(molfile, get_max_atoms(method))
    opt_result = run_xtb_opt(atoms, method=method)
    result = run_xtb_ir(opt_result.atoms, method=method, mol=mol)
    ir_from_molfile_cache.set(myhash, result, expire=None)
    return result


def ir_from_molfile(molfile, method):
    myhash = hash_object(molfile + method)

    result = ir_from_molfile_cache.get(myhash)

    if result is None:
        result = calculate_from_molfile(molfile, method, myhash)
    return result


def compile_all_bond_displacements(mol, atoms, ir):
    bond_displacements = []
    for mode_number in range(3 * len(ir.indices)):
        bond_displacements.append(
            get_bond_displacements(mol, atoms, ir.get_mode(mode_number))
        )

    return np.vstack(bond_displacements)


def clean_frequency(frequencies, n):
    if frequencies[n].imag != 0:
        c = "i"
        freq = frequencies[n].imag

    else:
        freq = frequencies[n].real
        c = " "
    return freq, c


def compile_modes_info(
    ir, linear, alignments, bond_displacements=None, bonds=None, raman_intensities=None
):
    frequencies = ir.get_frequencies()
    symbols = ir.atoms.get_chemical_symbols()
    modes = []
    sorted_alignments = sorted(alignments, reverse=True)
    mapping = dict(zip(np.arange(len(frequencies)), np.argsort(frequencies)))
    third_best_alignment = sorted_alignments[2]
    has_imaginary = False
    has_large_imaginary = False
    num_modes = 3 * len(ir.indices)
    if raman_intensities is None:
        raman_intensities = [None] * num_modes
    for n in range(num_modes):
        n = int(mapping[n])
        if n < 3:
            # print("below 5", alignments[n])
            # if alignments[n] >= third_best_alignment:
            modeType = "translation"
            # else:
            #     modeType = "rotation"
        elif n < 5:
            modeType = "rotation"
        elif n == 5:
            if linear:
                modeType = "vibration"
            else:
                if alignments[n] >= third_best_alignment:
                    modeType = "translation"
                else:
                    modeType = "rotation"
        else:
            modeType = "vibration"

        f, c = clean_frequency(frequencies, n)
        if c == "i":
            has_imaginary = True
            if f > IMAGINARY_FREQ_THRESHOLD:
                has_large_imaginary = True
        mostContributingBonds = None
        if bond_displacements is not None:
            mostContributingBonds = select_most_contributing_bonds(
                bond_displacements[n, :]
            )
            mostContributingBonds = [bonds[i] for i in mostContributingBonds]
            mode = ir.get_mode(n)

        ramanIntensity = float(raman_intensities[n]) if raman_intensities[n] is not None else None 
        modes.append(
            {
                "number": n,
                "displacements": get_displacement_xyz_for_mode(
                    ir, frequencies, symbols, n
                ),
                "intensity": float(ir.intensities[n]),
                "ramanIntensity": ramanIntensity,
                "wavenumber": float(f),
                "imaginary": True if c == "i" else False,
                "mostDisplacedAtoms": [
                    int(i)
                    for i in np.argsort(np.linalg.norm(mode - mode.sum(axis=0), axis=1))
                ][::-1],
                "mostContributingAtoms": [
                    int(i) for i in select_most_contributing_atoms(ir, n)
                ],
                "mostContributingBonds": mostContributingBonds,
                "modeType": modeType,
                "centerOfMassDisplacement": float(
                    np.linalg.norm(ir.get_mode(n).sum(axis=0))
                ),
                "totalChangeOfMomentOfInteria": get_change_in_moi(ir.atoms, ir, n),
                "displacementAlignment": alignments[n],
            }
        )

    return modes, has_imaginary, has_large_imaginary


def get_max_displacements(ir, linear):
    mode_abs_displacements = []

    for n in range(3 * len(ir.indices)):
        mode_abs_displacements.append(np.linalg.norm(ir.get_mode(n), axis=1))

    mode_abs_displacements = np.stack(mode_abs_displacements)
    if linear:
        mode_abs_displacements[:5, :] = 0
    else:
        mode_abs_displacements[:6, :] = 0

    return dict(
        zip(
            ir.indices,
            [list(a)[::-1] for a in mode_abs_displacements.argsort(axis=0).T],
        )
    )


def get_alignment(ir, mode_number):
    dot_result = []

    displacements = ir.get_mode(mode_number)

    for i, displ_i in enumerate(displacements):
        for j, displ_j in enumerate(displacements):
            if i < j:
                dot_result.append(spatial.distance.cosine(displ_i, displ_j))

    return np.mean(dot_result)


def get_displacement_xyz_for_mode(ir, frequencies, symbols, n):
    xyz_file = []
    xyz_file.append("%6d\n" % len(ir.atoms))

    f, c = clean_frequency(frequencies, n)

    xyz_file.append("Mode #%d, f = %.1f%s cm^-1" % (n, float(f.real), c))

    if ir.ir:
        xyz_file.append(", I = %.4f (D/Å)^2 amu^-1.\n" % ir.intensities[n])
    else:
        xyz_file.append(".\n")

    # dict_label = xyz_file[-1] + xyz_file[-2]
    # dict_label = dict_label.strip('\n')

    mode = ir.get_mode(n)
    for i, pos in enumerate(ir.atoms.positions):
        xyz_file.append(
            "%2s %12.5f %12.5f %12.5f %12.5f %12.5f %12.5f\n"
            % (symbols[i], pos[0], pos[1], pos[2], mode[i, 0], mode[i, 1], mode[i, 2],)
        )

    xyz_file_string = "".join(xyz_file)
    return xyz_file_string


def get_displacement_xyz_dict(ir):
    symbols = ir.atoms.get_chemical_symbols()
    frequencies = ir.get_frequencies()
    modes = {}

    for n in range(3 * len(ir.indices)):
        modes[n] = get_displacement_xyz_for_mode(ir, frequencies, symbols, n)

    return modes


def select_most_contributing_atoms(ir, mode, threshold: float = 0.4):
    displacements = ir.get_mode(mode)
    relative_contribution = (
        np.linalg.norm(displacements, axis=1)
        / np.linalg.norm(displacements, axis=1).max()
    )
    res = np.where(
        relative_contribution
        > threshold * np.max(np.abs(np.diff(relative_contribution)))
    )[0]

    return res


def select_most_contributing_bonds(displacements, threshold: float = 0.4):

    if len(displacements) > 1:
        relative_contribution = displacements / displacements.sum()
        return np.where(
            relative_contribution
            > threshold * np.max(np.abs(np.diff(relative_contribution)))
        )[0]
    else:
        return np.array([0])


@lru_cache()
def get_bonds_from_mol(mol) -> List[Tuple[int, int]]:
    all_bonds = []
    for i in range(mol.GetNumBonds()):
        bond = mol.GetBondWithIdx(i)
        all_bonds.append((bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()))

    return all_bonds


def get_bond_vector(positions, bond):
    return positions[bond[1]] - positions[bond[0]]


def get_displaced_positions(positions, mode):
    return positions + mode


def get_bond_displacements(mol, atoms, mode):
    bonds = get_bonds_from_mol(mol)
    positions = atoms.positions
    displaced_positions = get_displaced_positions(positions, mode) - mode.sum(axis=0)
    changes = []

    for bond in bonds:
        bond_displacements = np.linalg.norm(
            get_bond_vector(positions, bond)
        ) - np.linalg.norm(get_bond_vector(displaced_positions, bond))

        changes.append(np.linalg.norm(bond_displacements))

    return changes


def get_change_in_moi(atoms, ir, mode_number):
    return np.linalg.norm(
        np.linalg.norm(
            get_moments_of_inertia(
                get_displaced_positions(atoms.positions, ir.get_mode(mode_number)),
                atoms.get_masses(),
            )
        )
        - np.linalg.norm(get_moments_of_inertia(atoms.positions, atoms.get_masses()))
    )
