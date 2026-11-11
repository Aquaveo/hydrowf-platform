"""Workflow -> Argo translation, on small fixture CWLs written per test."""
import pytest

from hydrowf_platform.translator import UnsupportedCWL, translate

MAKE = {   # like ngiab-preprocess: baseCommand, staged File, empty marker file, output named by an input
    "cwlVersion": "v1.2", "class": "CommandLineTool", "id": "make",
    "requirements": {
        "DockerRequirement": {"dockerPull": "img/make:1"},
        "InitialWorkDirRequirement": {"listing": [
            {"entry": "$(inputs.data)", "entryname": ".cache/data.gpkg"},
            {"entry": "", "entryname": ".cache/no_update"},
        ]},
        "ResourceRequirement": {"coresMin": 2, "ramMin": 512},
    },
    "baseCommand": ["cli"],
    "arguments": [{"prefix": "-o", "valueFrom": "$(runtime.outdir)/$(inputs.name)"}],
    "inputs": {"data": {"type": "File"}, "name": {"type": "string", "default": "run"}},
    "outputs": {"out": {"type": "Directory", "outputBinding": {"glob": "$(inputs.name)"}}},
}
USE = {    # like ngiab-to-cng: image ENTRYPOINT, Directory input, output named after the input
    "cwlVersion": "v1.2", "class": "CommandLineTool", "id": "use",
    "requirements": {"DockerRequirement": {"dockerPull": "img/use:1"}},
    "arguments": [{"prefix": "--output", "valueFrom": "$(runtime.outdir)/$(inputs.run.basename)_done"}],
    "inputs": {"run": {"type": "Directory", "inputBinding": {"position": 1}}},
    "outputs": {"done": {"type": "Directory", "outputBinding": {"glob": "$(inputs.run.basename)_done"}}},
}
WORKFLOW = {
    "cwlVersion": "v1.2", "class": "Workflow", "id": "two_steps",
    "inputs": {"data": "File", "name": "string"},
    "steps": {
        "make_it": {"run": "make.cwl", "in": {"data": "data", "name": "name"}, "out": ["out"]},
        "use_it": {"run": "use.cwl", "in": {"run": "make_it/out"}, "out": ["done"]},
    },
    "outputs": {},
}
JOB = {"data": {"class": "File", "location": "s3://my-bucket/inputs/data.gpkg"}, "name": "r1"}
ENTRYPOINTS = {"img/make:1": [], "img/use:1": ["python", "/app/use.py"]}


@pytest.fixture
def workflow(write_cwl):
    def build(make=MAKE, use=USE, wf=WORKFLOW):
        write_cwl("make.cwl", make)
        write_cwl("use.cwl", use)
        return write_cwl("wf.cwl", wf)
    return build


def run(path, job=JOB):
    return translate(path, job, entrypoint_lookup=ENTRYPOINTS.__getitem__)


def templates(argo):
    return {t["name"]: t for t in argo["spec"]["templates"]}


def test_dag_wires_steps_through_artifacts(workflow):
    argo = run(workflow())
    tasks = {t["name"]: t for t in templates(argo)["main"]["dag"]["tasks"]}
    assert argo["metadata"]["generateName"] == "two-steps-"
    assert "dependencies" not in tasks["make-it"]
    assert tasks["make-it"]["arguments"]["artifacts"] == [{"name": "data", "s3": {"key": "inputs/data.gpkg"}}]
    assert tasks["use-it"]["dependencies"] == ["make-it"]
    assert tasks["use-it"]["arguments"]["artifacts"] == [
        {"name": "run", "from": "{{tasks.make-it.outputs.artifacts.out}}"}]


def test_initial_workdir_entries_become_artifacts_in_the_output_dir(workflow):
    make = templates(run(workflow()))["make-it"]
    assert make["inputs"]["artifacts"] == [
        {"name": "data", "path": "/cwl-output/.cache/data.gpkg"},
        {"name": "iwd-1", "path": "/cwl-output/.cache/no_update", "raw": {"data": ""}},
    ]
    assert make["outputs"]["artifacts"] == [{"name": "out", "path": "/cwl-output/r1"}]


def test_other_inputs_go_under_cwl_inputs_and_basenames_flow_downstream(workflow):
    use = templates(run(workflow()))["use-it"]
    assert use["inputs"]["artifacts"] == [{"name": "run", "path": "/cwl-inputs/run/r1"}]
    assert use["outputs"]["artifacts"] == [{"name": "done", "path": "/cwl-output/r1_done"}]


def test_command_is_entrypoint_or_base_command(workflow):
    t = templates(run(workflow()))
    assert t["make-it"]["container"]["command"] == ["cli"]
    assert t["make-it"]["container"]["args"] == ["-o", "/cwl-output/r1"]
    assert t["use-it"]["container"]["command"] == ["python", "/app/use.py"]
    assert t["use-it"]["container"]["args"] == ["--output", "/cwl-output/r1_done", "/cwl-inputs/run/r1"]


def test_container_runs_in_the_output_dir_with_resources(workflow):
    c = templates(run(workflow()))["make-it"]["container"]
    assert c["workingDir"] == "/cwl-output"
    assert {"name": "HOME", "value": "/cwl-output"} in c["env"]
    assert c["resources"] == {"requests": {"cpu": "2", "memory": "512Mi"}}
    assert c["volumeMounts"] == [{"name": "cwl-output", "mountPath": "/cwl-output"}]


def test_step_default_overrides_tool_default(workflow):
    wf = {**WORKFLOW, "steps": {**WORKFLOW["steps"],
                                "make_it": {"run": "make.cwl", "in": {"data": "data", "name": {"default": "other"}},
                                            "out": ["out"]}}}
    make = templates(run(workflow(wf=wf)))["make-it"]
    assert make["outputs"]["artifacts"] == [{"name": "out", "path": "/cwl-output/other"}]


@pytest.mark.parametrize("glob", ["*_done", "r?", "[ab]"])
def test_wildcard_glob_is_rejected(workflow, glob):
    use = {**USE, "outputs": {"done": {"type": "Directory", "outputBinding": {"glob": glob}}}}
    with pytest.raises(UnsupportedCWL, match="not an exact path"):
        run(workflow(use=use))


def test_input_without_s3_location_is_rejected(workflow):
    with pytest.raises(UnsupportedCWL, match="s3://"):
        run(workflow(), job={**JOB, "data": {"class": "File", "location": "file:///data.gpkg"}})


def test_no_entrypoint_and_no_base_command_is_rejected(workflow):
    make = {k: v for k, v in MAKE.items() if k != "baseCommand"}
    with pytest.raises(UnsupportedCWL, match="no ENTRYPOINT"):
        run(workflow(make=make))


@pytest.mark.parametrize("feature", ["scatter", "when"])
def test_unsupported_step_features_are_rejected(workflow, feature):
    step = {**WORKFLOW["steps"]["use_it"], feature: "run"}
    wf = {**WORKFLOW, "steps": {**WORKFLOW["steps"], "use_it": step}}
    with pytest.raises(UnsupportedCWL, match=feature):
        run(workflow(wf=wf))


def test_javascript_expression_is_rejected(workflow):
    use = {**USE, "arguments": [{"valueFrom": "$(inputs.run.basename.toUpperCase())"}]}
    with pytest.raises(UnsupportedCWL):
        run(workflow(use=use))
