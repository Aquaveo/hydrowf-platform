"""Translate a CWL v1.2 Workflow (the hydrowf-tools subset) plus a job into an Argo Workflow.

Supported subset (hydrowf-tools hints/README.md): CommandLineTool, and Workflow with plain
steps; parameter references only (no JavaScript). Everything is resolved at translation
time, so each step becomes one concrete Argo container template.

Conventions, matching what cwltool does with the hydrowf tools:

- ``runtime.outdir`` is ``dockerOutputDirectory`` or ``/cwl-output`` (an emptyDir), also the working directory and HOME
  (cwltool sets HOME to the output dir; ngiab-preprocess finds the hydrofabric through it).
- InitialWorkDir entries are artifacts under ``runtime.outdir`` (``entryname``, or the basename);
  other File/Directory inputs go under ``/cwl-inputs/<input>/``.
- Workflow-level File/Directory inputs have ``location: s3://<bucket>/<key>`` in the job;
  Argo reads the key from its default artifact repository.
- Command line: ``arguments`` (in order) then inputs (by name), sorted by ``position`` first.
- The container ``command`` is the image's ENTRYPOINT, or ``baseCommand`` if the image has
  none: Argo's emissary executor needs it, and otherwise asks the image registry.

Anything outside the subset raises :class:`UnsupportedCWL`.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

OUTDIR = "/cwl-output"   # default; a tool's DockerRequirement.dockerOutputDirectory overrides it
INDIR = "/cwl-inputs"
REF = re.compile(r"\$\(([^)]*)\)")
FILE_CLASSES = ("File", "Directory")

EntrypointLookup = Callable[[str], list[str]]


class UnsupportedCWL(ValueError):
    """The CWL uses something the translator does not map to Argo."""


def docker_entrypoint(image: str) -> list[str]:
    """The image's ENTRYPOINT, from the local Docker daemon."""
    out = subprocess.run(["docker", "image", "inspect", "-f", "{{json .Config.Entrypoint}}", image],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise UnsupportedCWL(f"cannot inspect {image}: {out.stderr.strip()}")
    return json.loads(out.stdout) or []


def k8s_name(name: str) -> str:
    return name.replace("_", "-").lower()


def load(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def is_file_like(value: Any) -> bool:
    return isinstance(value, dict) and value.get("class") in FILE_CLASSES


def requirement(tool: dict, cls: str) -> dict | None:
    for section in ("requirements", "hints"):
        reqs = tool.get(section) or {}
        if isinstance(reqs, dict):
            if cls in reqs:
                return reqs[cls]
        else:
            for r in reqs:
                if r.get("class") == cls:
                    return r
    return None


def input_defs(tool: dict) -> dict[str, dict]:
    ins = tool.get("inputs") or {}
    if isinstance(ins, list):
        return {i["id"]: i for i in ins}
    return {k: (v if isinstance(v, dict) else {"type": v}) for k, v in ins.items()}


def output_defs(tool: dict) -> dict[str, dict]:
    outs = tool.get("outputs") or {}
    if isinstance(outs, list):
        return {o["id"]: o for o in outs}
    return outs


# ---------------------------------------------------------------- parameter references

def lookup(expr: str, ctx: dict) -> Any:
    """Evaluate inputs.x, inputs.x.basename, inputs.x.path, runtime.outdir/cores, self."""
    parts = expr.strip().split(".")
    if parts[0] == "self":
        val, rest = ctx["self"], parts[1:]
    elif parts[0] == "runtime" and len(parts) == 2 and parts[1] in ctx["runtime"]:
        return ctx["runtime"][parts[1]]
    elif parts[0] == "inputs" and len(parts) >= 2:
        val, rest = ctx["inputs"].get(parts[1]), parts[2:]
    else:
        raise UnsupportedCWL(f"unsupported reference $({expr})")
    for p in rest:
        if not isinstance(val, dict) or p not in val:
            raise UnsupportedCWL(f"cannot resolve $({expr})")
        val = val[p]
    return val


def evaluate(text: Any, ctx: dict) -> Any:
    """A lone $(...) returns the value itself (may be a File object); otherwise interpolate."""
    if not isinstance(text, str):
        return text
    m = REF.fullmatch(text.strip())
    if m:
        return lookup(m.group(1), ctx)
    return REF.sub(lambda mm: as_arg(lookup(mm.group(1), ctx)), text)


def as_arg(val: Any) -> str:
    if is_file_like(val):
        return val["path"]
    if isinstance(val, bool):
        return str(val).lower()
    return str(val)


# ---------------------------------------------------------------- command line

def binding_parts(binding: dict, value: Any) -> list[str]:
    """One argument or input binding -> command-line words."""
    if value is None or value is False:
        return []
    prefix = binding.get("prefix")
    if value is True:
        return [prefix] if prefix else []
    word = as_arg(value)
    if prefix:
        return [prefix, word] if binding.get("separate", True) else [prefix + word]
    return [word]


def command_line(tool: dict, ctx: dict) -> list[str]:
    """arguments + inputBindings, in cwltool's order: position, then arguments before inputs
    (arguments in listed order, inputs by name)."""
    keyed = []
    for i, arg in enumerate(tool.get("arguments") or []):
        if isinstance(arg, str):
            keyed.append(((0, 0, i), [as_arg(evaluate(arg, ctx))]))
        else:
            value = evaluate(arg.get("valueFrom"), ctx)
            keyed.append(((arg.get("position", 0), 0, i), binding_parts(arg, value)))
    for name, d in sorted(input_defs(tool).items()):
        b = d.get("inputBinding")
        if b is None:
            continue
        value = ctx["inputs"].get(name)
        if value is not None and "valueFrom" in b:
            value = evaluate(b["valueFrom"], {**ctx, "self": value})
        keyed.append(((b.get("position", 0), 1, name), binding_parts(b, value)))
    words: list[str] = []
    for _, parts in sorted(keyed, key=lambda kv: kv[0]):
        words.extend(parts)
    return words


# ---------------------------------------------------------------- tool -> template

def resolve_inputs(tool: dict, given: dict) -> dict:
    """Job values over defaults; File/Directory values are copied (paths are set per template)."""
    values = {}
    for name, d in input_defs(tool).items():
        v = given.get(name, d.get("default"))
        values[name] = dict(v) if isinstance(v, dict) else v
    return values


def tool_template(name: str, tool: dict, given: dict, sources: dict,
                  entrypoint_lookup: EntrypointLookup) -> tuple[dict, list[dict], dict]:
    """One CommandLineTool -> (Argo template, task artifact arguments, output info).

    given: input values; sources: input name -> Argo artifact source for File/Directory inputs.
    """
    docker = requirement(tool, "DockerRequirement")
    if not docker or "dockerPull" not in docker:
        raise UnsupportedCWL(f"{name}: needs DockerRequirement.dockerPull")
    res = requirement(tool, "ResourceRequirement") or {}
    outdir = docker.get("dockerOutputDirectory", OUTDIR)
    cores = res.get("coresMin", 1)
    inputs = resolve_inputs(tool, given)
    ctx = {"inputs": inputs, "runtime": {"outdir": outdir, "cores": cores}, "self": None}

    artifacts: list[dict] = []
    arg_artifacts: list[dict] = []

    def add_artifact(input_name: str, path: str) -> None:
        artifacts.append({"name": k8s_name(input_name), "path": path})
        arg_artifacts.append({"name": k8s_name(input_name), **sources[input_name]})

    staged = set()
    iwd = requirement(tool, "InitialWorkDirRequirement") or {}
    for n, entry in enumerate(iwd.get("listing") or []):
        value = evaluate(entry.get("entry"), ctx)
        if is_file_like(value):
            ref = REF.fullmatch(entry["entry"].strip())
            src_name = ref.group(1).split(".")[1] if ref else None
            if src_name not in sources:
                raise UnsupportedCWL(f"{name}: InitialWorkDir entry must be $(inputs.<File/Directory>)")
            target = evaluate(entry["entryname"], ctx) if entry.get("entryname") else value["basename"]
            value["path"] = f"{outdir}/{target}"
            value["basename"] = Path(target).name
            add_artifact(src_name, value["path"])
            staged.add(src_name)
        elif isinstance(value, str) and entry.get("entryname"):
            target = evaluate(entry["entryname"], ctx)
            artifacts.append({"name": f"iwd-{n}", "path": f"{outdir}/{target}", "raw": {"data": value}})
        else:
            raise UnsupportedCWL(f"{name}: unsupported InitialWorkDir entry {entry}")

    for in_name, v in inputs.items():
        if is_file_like(v) and in_name not in staged:
            v["path"] = f"{INDIR}/{in_name}/{v['basename']}"
            add_artifact(in_name, v["path"])

    base = tool.get("baseCommand") or []
    base = [base] if isinstance(base, str) else list(base)
    words = command_line(tool, ctx)
    entrypoint = entrypoint_lookup(docker["dockerPull"])
    if entrypoint:
        command, args = list(entrypoint), base + words
    elif base:
        command, args = base, words
    else:
        raise UnsupportedCWL(f"{name}: image has no ENTRYPOINT and the tool has no baseCommand")

    outputs, out_info = [], {}
    for out_name, d in output_defs(tool).items():
        if d.get("type") not in FILE_CLASSES:
            raise UnsupportedCWL(f"{name}.{out_name}: only File/Directory outputs are supported")
        glob = evaluate(d["outputBinding"]["glob"], ctx)
        if not isinstance(glob, str) or any(c in glob for c in "*?["):
            raise UnsupportedCWL(f"{name}.{out_name}: glob {glob!r} is not an exact path "
                                 "(an Argo output artifact needs one path)")
        outputs.append({"name": k8s_name(out_name), "path": f"{outdir}/{glob}"})
        out_info[out_name] = {"class": d["type"], "basename": Path(glob).name}

    template = {
        "name": name,
        "inputs": {"artifacts": artifacts} if artifacts else {},
        "outputs": {"artifacts": outputs},
        "volumes": [{"name": "cwl-output", "emptyDir": {}}],
        "container": {
            "image": docker["dockerPull"],
            "command": command,
            "args": args,
            "workingDir": outdir,
            "env": [{"name": "HOME", "value": outdir}],
            "resources": {"requests": {"cpu": str(cores), "memory": f"{res.get('ramMin', 1024)}Mi"}},
            "volumeMounts": [{"name": "cwl-output", "mountPath": outdir}],
        },
    }
    return template, arg_artifacts, out_info


# ---------------------------------------------------------------- workflow -> DAG

def s3_source(value: dict) -> dict:
    loc = value.get("location", "")
    if not loc.startswith("s3://") or loc.count("/") < 3:
        raise UnsupportedCWL(f"workflow File/Directory inputs need an s3://<bucket>/<key> location, got {loc!r}")
    return {"s3": {"key": loc.split("/", 3)[3]}}


def translate(workflow_path: str | Path, job: dict,
              entrypoint_lookup: EntrypointLookup = docker_entrypoint) -> dict:
    """CWL Workflow + job values -> Argo Workflow (as a dict)."""
    wf_path = Path(workflow_path)
    wf = load(wf_path)
    if wf.get("class") != "Workflow":
        raise UnsupportedCWL(f"{wf_path.name}: expected class Workflow, got {wf.get('class')}")

    wf_inputs = {}
    for name, d in input_defs(wf).items():
        v = job.get(name, d.get("default"))
        if is_file_like(v):
            v = {**v, "basename": v.get("basename") or v["location"].rstrip("/").rsplit("/", 1)[-1]}
        wf_inputs[name] = v

    templates, tasks, step_outputs = [], [], {}
    for step_name, step in wf["steps"].items():
        for unsupported in ("scatter", "when"):
            if unsupported in step:
                raise UnsupportedCWL(f"step {step_name}: `{unsupported}` is not supported")
        tool = load(wf_path.parent / step["run"])
        if tool.get("class") != "CommandLineTool":
            raise UnsupportedCWL(f"step {step_name}: only CommandLineTool steps are supported")
        given, sources, deps = {}, {}, []
        for in_name, src in (step.get("in") or {}).items():
            if isinstance(src, dict):
                if "source" not in src:
                    given[in_name] = src.get("default")
                    continue
                src = src["source"]
            if "/" in src:
                up_step, up_out = src.split("/")
                given[in_name] = dict(step_outputs[up_step][up_out])
                sources[in_name] = {"from": f"{{{{tasks.{k8s_name(up_step)}.outputs.artifacts.{k8s_name(up_out)}}}}}"}
                deps.append(k8s_name(up_step))
            else:
                v = wf_inputs[src]
                given[in_name] = dict(v) if isinstance(v, dict) else v
                if is_file_like(v):
                    sources[in_name] = s3_source(v)
        template, arg_artifacts, out_info = tool_template(k8s_name(step_name), tool, given, sources,
                                                          entrypoint_lookup)
        step_outputs[step_name] = out_info
        templates.append(template)
        task: dict = {"name": k8s_name(step_name), "template": template["name"]}
        if deps:
            task["dependencies"] = sorted(set(deps))
        if arg_artifacts:
            task["arguments"] = {"artifacts": arg_artifacts}
        tasks.append(task)

    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Workflow",
        "metadata": {"generateName": f"{k8s_name(wf.get('id', wf_path.stem))}-",
                     "annotations": {"hydrowf/source": wf_path.name}},
        "spec": {"entrypoint": "main",
                 "templates": [{"name": "main", "dag": {"tasks": tasks}}] + templates},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hydrowf-cwl2argo", description=__doc__.splitlines()[0])
    parser.add_argument("workflow", type=Path, help="CWL Workflow file")
    parser.add_argument("job", type=Path, help="job file (YAML); File inputs as s3:// locations")
    parser.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    args = parser.parse_args(argv)
    try:
        argo = translate(args.workflow, load(args.job))
    except UnsupportedCWL as e:
        print(f"hydrowf-cwl2argo: {e}", file=sys.stderr)
        return 2
    text = yaml.safe_dump(argo, sort_keys=False, width=200)
    if args.output:
        args.output.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
