# hydrowf-platform

hydrowf backend: OGC API - Processes plugins, CWL-to-Argo translator, publishing.

So far: the **CWL → Argo translator** (`hydrowf_platform.translator`). It turns a pipeline from
[hydrowf-tools](https://github.com/Aquaveo/hydrowf-tools) plus a job into an Argo Workflow.
The tools are described in CWL; Argo is the runtime. What maps and what we had to change is in
[docs/argo-findings.md](docs/argo-findings.md).

## Use

```sh
uv run hydrowf-cwl2argo ../hydrowf-tools/pipelines/ngiab-simulation.cwl examples/ngiab-simulation.yml -o sim.yaml
argo submit -n argo --watch sim.yaml
```

- The job is the pipeline's usual job, except that File/Directory inputs are S3 locations
  (`location: s3://my-bucket/<key>`); Argo reads them from its default artifact repository.
- The translator reads each image's ENTRYPOINT with `docker image inspect`, so the images must
  be on the machine that runs it.
- Exit code 2: the CWL uses something outside the supported subset (hydrowf-tools
  `hints/README.md`); the message says what.

### Local cluster

kind + Argo Workflows v4.1.6 + RustFS as the S3 artifact store: see hydrowf-deploy. The example
jobs expect the hydrofabric in the artifact bucket:

```sh
aws --endpoint-url http://localhost:9000 s3 cp conus_nextgen.gpkg          s3://my-bucket/hydrofabric/v2.2/
aws --endpoint-url http://localhost:9000 s3 cp conus_igraph_network.gpickle s3://my-bucket/hydrofabric/v2.2/
kind load docker-image --name hydrowf hydrowf/ngiab-preprocess:4.9.5.dev2-02c4145 \
  hydrowf/ngiab-calibrate:0.2.0 hydrowf/ngiab-to-cng:0.1.0     # local-only images
```

## Tests

```sh
uv run pytest                                          # unit tests: no Docker, no cluster
HYDROWF_TOOLS=../hydrowf-tools uv run pytest           # + the real hydrowf-tools pipelines
```

CI checks out hydrowf-tools `main` for the integration tests. Their expected command lines are
the ones cwltool ran for the same tools and job, and that ran on Argo.
