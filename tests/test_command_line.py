"""Command-line construction must match cwltool's: these are the rules cwltool applied to the
hydrowf tools (its cache keys log the exact command lines)."""
from hydrowf_platform.translator import OUTDIR, command_line


def ctx(inputs, cores=1):
    return {"inputs": inputs, "runtime": {"outdir": OUTDIR, "cores": cores}, "self": None}


def test_arguments_in_order_then_inputs_by_name():
    tool = {
        "arguments": [{"prefix": "--out", "valueFrom": "$(runtime.outdir)/x"}, "-s", "-f"],
        "inputs": {
            "start": {"type": "string", "inputBinding": {"prefix": "--start"}},
            "end": {"type": "string", "inputBinding": {"prefix": "--end"}},
            "feature": {"type": "string", "inputBinding": {"prefix": "-i"}},
            "run_name": {"type": "string"},           # no binding: not on the command line
        },
    }
    words = command_line(tool, ctx({"start": "2017", "end": "2019", "feature": "gage-1", "run_name": "r"}))
    assert words == ["--out", "/cwl-output/x", "-s", "-f", "--end", "2019", "-i", "gage-1", "--start", "2017"]


def test_position_comes_first():
    tool = {
        "arguments": [{"position": 1, "valueFrom": "$(runtime.outdir)/$(inputs.run.basename)_out"}],
        "inputs": {
            "run": {"type": "Directory", "inputBinding": {"position": 0}},
            "gage": {"type": "string", "inputBinding": {"position": 2}},
            "n": {"type": "int", "inputBinding": {"position": 3}},
        },
    }
    run = {"class": "Directory", "basename": "r1", "path": "/cwl-inputs/run/r1"}
    words = command_line(tool, ctx({"run": run, "gage": "0101", "n": 3}))
    assert words == ["/cwl-inputs/run/r1", "/cwl-output/r1_out", "0101", "3"]


def test_null_and_booleans():
    tool = {"inputs": {
        "flag_on": {"type": "boolean", "inputBinding": {"prefix": "--on"}},
        "flag_off": {"type": "boolean", "inputBinding": {"prefix": "--off"}},
        "optional": {"type": "string?", "inputBinding": {"prefix": "--opt"}},
    }}
    assert command_line(tool, ctx({"flag_on": True, "flag_off": False, "optional": None})) == ["--on"]


def test_value_from_with_self():
    tool = {"inputs": {"model": {"type": "string", "inputBinding": {"valueFrom": "--$(self)"}}}}
    assert command_line(tool, ctx({"model": "lstm"})) == ["--lstm"]
    assert command_line(tool, ctx({"model": None})) == []


def test_prefix_without_separator():
    tool = {"inputs": {"x": {"type": "int", "inputBinding": {"prefix": "-n=", "separate": False}}}}
    assert command_line(tool, ctx({"x": 4})) == ["-n=4"]


def test_runtime_cores():
    tool = {"arguments": ["$(runtime.cores)"]}
    assert command_line(tool, ctx({}, cores=4)) == ["4"]
