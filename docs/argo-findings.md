# CWL → Argo: findings

From translating and running both hydrowf-tools pipelines on kind + Argo Workflows v4.1.6 +
RustFS (2026-11-11), gage-01031510, 2017-10 to 2019-09.

| Pipeline | Argo | Result |
|---|---|---|
| `ngiab-simulation.cwl` | preprocess → run → to-cng | succeeded, 11 min. Same 214 output files as the cwltool run; the catchment, nexus, forcing and t-route time series are identical. |
| `ngiab-calibration.cwl` (3 iterations) | preprocess → calibrate → run → to-cng | succeeded, 27 min. `model_params` for SLOTH, NoahOWP and CFE; `cng` with `configuration_name ngiab_cfe_calibrated`. |

## What maps directly

| CWL | Argo |
|---|---|
| `Workflow` with plain steps | DAG; `dependencies` from `step/output` sources |
| `File`/`Directory` from the job (`s3://` location) | input artifact, S3 key in the default artifact repository |
| `Directory` between steps | output artifact (`.tgz`) → `from: {{tasks.X.outputs.artifacts.Y}}` |
| `InitialWorkDirRequirement` (`entryname`, empty file) | artifact at that path under the output dir; `raw: {data: ""}` |
| `$(runtime.outdir)`, `$(inputs.x.basename)`, `$(runtime.cores)` | resolved at translation time |
| `ResourceRequirement` | `resources.requests` |
| `NetworkAccess` | nothing to do (pods have network) |

The command line follows cwltool's order (position, then arguments before inputs, inputs by
name); the preprocess and run command lines are word-for-word what cwltool ran.

## What we had to change

- **`command`, not only `args`.** Argo's emissary executor needs the container command. With
  `args` only it asks the registry for the image's ENTRYPOINT, which failed for local images
  ("failed to look-up entrypoint/cmd ... UNAUTHORIZED"). The translator sets `command` to the
  ENTRYPOINT (or `baseCommand`) and the rest goes in `args`: what `docker run image <args>` does.
- **Exact globs.** An Argo output artifact needs one path, so `outputBinding.glob` can't be a
  wildcard. ngiab-calibrate used `*_calibrated`; it now copies its input to an exact
  `<run>_calibrated` (hydrowf-tools #8).
- **HOME.** cwltool sets HOME to the output dir and ngiab-preprocess finds the hydrofabric
  through it, so templates set `HOME=/cwl-output`.

## cwltool-only

`loadListing: deep_listing` in the tools only makes cwltool's `--cachedir` work; Argo ignores it.
`dockerOutputDirectory` sets the output dir for both (the translator defaults to `/cwl-output`).
Argo's caching equivalent is `memoize`, with a key the translator
will have to compute (inputs, image digests).

## Open

- **Reusable templates:** values are resolved per job, so every submit needs a translation.
  For OGC processes, scalar inputs should become Argo parameters (one WorkflowTemplate per pipeline).
- **ENTRYPOINT lookup** from a registry (image config) instead of local Docker.
- **Images:** publish them, so the cluster doesn't need `kind load`.
- **memoize**, `EnvVarRequirement`, File outputs.
- **Hydrofabric per pod:** every preprocess pod copies the 4.9 GB gpkg from S3; a shared
  catalog (icefabric) or volume would avoid it.
