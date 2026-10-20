# Binary-crystal heat transport

## Scientific target and fields

Predict 300 K total lattice thermal conductivity: particle plus coherence contributions to Wigner transport in the single-mode relaxation-time approximation. Use the scalar average of the xx, yy and zz tensor components, in W m^-1 K^-1. Electronic conductivity is excluded. The target follows frozen PBE calculations without non-analytical corrections (NAC); it is not experimental conductivity or an infinite-size exact result.

The 103 geometries cover 37 rocksalt, 33 zincblende and 33 wurtzite binary crystals. `structures.jsonl.gz` is gzip JSON Lines. Each record contains:

- `material_id`: anonymous immutable output ID.
- `atomic_numbers`: ordered integer element identities.
- `positions_angstrom`: Cartesian positions, N by 3, in angstrom.
- `cell_angstrom`: 3 by 3 lattice matrix, one lattice vector per row.
- `pbc`: three periodic flags.
- `temperature_K`: 300 K.
- `fc2_supercell` and `fc3_supercell`: integer replication matrices for second- and third-order calculations. These are not reference force constants.
- `primitive_matrix`: a primitive-basis transformation, or `"auto"` where the source gives none explicitly.
- `q_point_mesh`: the reference reciprocal-space integration mesh.

`calculation_settings.json` specifies the physical convention. The template has all test IDs and blank predictions. Return exactly `material_id,kappa_W_mK`, one finite nonnegative value per ID. Approximate methods are allowed; retain a record of differences from the supplied reference settings. Failures require a label-free fallback and remain in the score. Mean symmetric relative error is `2*abs(pred-ref)/(pred+ref)`, with zero for two zero values. No training conductivity labels or mid-run scientific checks are provided.

## Allowed scientific model and runtime

The checkpoint `model_weights/chgnet_0.3.0_e29f68s314m37.pth.tar` is CHGNet 0.3.0, with 412,525 parameters. It provides energy in eV/atom, forces in eV/angstrom and stress in GPa. It was pretrained on Materials Project GGA/GGA+U energy, force, stress and magnetic-moment trajectories; training may include these materials. This evaluates downstream transport performance and does not assert unseen-material generalization.

The scientific libraries are isolated in `/opt/sie-research`. Invoke `/opt/sie-research/bin/python` explicitly. Load local weights with `CHGNet.from_file(checkpoint_path, mlp_out_bias=False, version="0.3.0")`; set the device to CPU when no GPU is allocated. A scientific model card and BSD-3-Clause notice accompany the checkpoint. No remote model download is required.

An actionable research route is:

1. Construct a pymatgen `Structure` from the full lattice, species and Cartesian positions. Evaluate energy/forces/stress and optionally relax with the allowed model.
2. Construct phono3py atoms using the same geometry and primitive transformation. FC3 uses `supercell_matrix`; FC2 can use a different `phonon_supercell_matrix`. Generate both displacement sets and supply separate forces when these cells differ.
3. Generate second- and third-order force constants, check finiteness and acoustic behavior, and investigate unstable modes. Acoustic sum rules, displacement choices, convergence control and force-constant corrections are possible research directions.
4. Solve phonons on the required mesh, using 300 K, `nac_params=None`, and Wigner transport. In phono3py 3.30.0 the API spelling is `transport_type="MS-SMM19"`, with `is_LBTE=False`. Read `kappa_TOT_RTA`, including particle and coherence terms; average the first three tensor components.
5. Produce a complete CSV. Record failures and approximations without using hidden references to choose them.

The reference settings can be computationally demanding. No full-dataset research runtime has been measured; numerical approximations and resource allocations must be disclosed. Public data and anonymous IDs cannot prove absence of model contamination. Published reference conductivities, reference force constants and per-sample model predictions are prohibited research resources.

## Attribution and license

Geometry and target construction derives from the thermal-transport dataset distributed with Matbench Discovery, DOI https://doi.org/10.6084/m9.figshare.22715158.v38, under CC BY 4.0. Retain `ATTRIBUTION.txt`. Changes include freezing the 103-case subset, anonymizing IDs, exporting initial scientific geometry/settings and separating targets from Agent input. The checkpoint/software notice is BSD-3-Clause; this does not relicense or redistribute its underlying Materials Project training corpus. Original authors do not endorse this package or submitted models.
