# Data provenance and scientific boundary

All lead structures, starting-material records, allowlists, route blueprints, and
reaction SMARTS in this task are small benchmark fixtures authored for
[approved Discussion #4](https://github.com/PrismaX-Team/LiveDiscoveryBench-Tasks/discussions/4).
They are not copied from a commercial catalog, reaction database, assay dataset,
PDBbind, CrossDocked, or a private model. The Proposal author confirmed the
permissions needed to prepare this public task contribution.

The verifier uses the open-source RDKit distribution, version 2025.09.6, for
parsing, cleanup, canonical tautomer selection, canonical isomeric SMILES,
reaction SMARTS execution, Morgan fingerprints, Tanimoto similarity, and 2D
descriptors. RDKit is BSD-licensed; see the
[RDKit repository](https://github.com/rdkit/rdkit) and the dependency metadata
installed in the container.

EGVR-Agent commit
`becf6e4ae6a472bf9c5c706351a2d8b32c89ba77` was inspected for task schemas,
budget/repair patterns, trace consistency, and RDKit property conventions. No
EGVR-Agent source code, model, external adapter, checkpoint, or dataset is copied
into this package. In particular, its synthesis proxies and generation adapters
are not used as route evidence.

Passing route replay means only that the submitted graph is the unique product
of each frozen, ordered reaction SMARTS on the allowed benchmark reactants. It
does not establish suitable experimental conditions, selectivity, yield,
availability, safety, purity, or laboratory synthesizability. The
`seed_similarity` objective is a transparent ligand-based activity-retention
surrogate and is not measured biological activity.
