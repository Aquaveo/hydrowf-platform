"""Integration: the real hydrowf-tools pipelines ($HYDROWF_TOOLS) with the example S3 jobs.

The expected command lines are what cwltool ran for the same tools and job values (its
--cachedir keys log them), and what ran successfully on Argo (kind, Argo v4.1.6, RustFS):
ngiab-simulation and ngiab-calibration, 2026-11-11.
"""
import pytest

from hydrowf_platform.translator import translate


def templates(argo):
    return {t["name"]: t for t in argo["spec"]["templates"]}


def full_command(template):
    return template["container"]["command"] + template["container"]["args"]


@pytest.fixture
def simulation(hydrowf_tools, example_job, entrypoints):
    return translate(hydrowf_tools / "pipelines/ngiab-simulation.cwl", example_job("ngiab-simulation.yml"),
                     entrypoint_lookup=entrypoints)


@pytest.fixture
def calibration(hydrowf_tools, example_job, entrypoints):
    return translate(hydrowf_tools / "pipelines/ngiab-calibration.cwl", example_job("ngiab-calibration.yml"),
                     entrypoint_lookup=entrypoints)


def test_simulation_dag(simulation):
    tasks = templates(simulation)["main"]["dag"]["tasks"]
    assert [(t["name"], t.get("dependencies", [])) for t in tasks] == [
        ("preprocess", []), ("run", ["preprocess"]), ("to-cng", ["run"])]


def test_preprocess_matches_cwltool(simulation):
    pre = templates(simulation)["preprocess"]
    assert full_command(pre) == [
        "cli", "--output_root", "/cwl-output", "-o", "/cwl-output/cfe-01031510", "-s", "-f", "-r",
        "--end_date", "2019-09-30", "-i", "gage-01031510", "--precip_max_gap", "6", "--source", "nwm",
        "--start_date", "2017-10-01", "--subset_type", "nexus"]
    # the hydrofabric lands where the preprocessor looks, with no_update so it never downloads it
    assert [a["path"] for a in pre["inputs"]["artifacts"]] == [
        "/cwl-output/.ngiab/hydrofabric/v2.2/conus_nextgen.gpkg",
        "/cwl-output/.ngiab/hydrofabric/v2.2/conus_igraph_network.gpickle",
        "/cwl-output/.ngiab/hydrofabric/v2.2/no_update"]
    assert {"name": "HOME", "value": "/cwl-output"} in pre["container"]["env"]


def test_run_and_to_cng_match_cwltool(simulation):
    t = templates(simulation)
    assert full_command(t["run"]) == ["/ngen/HelloNGEN.sh", "/cwl-output/cfe-01031510", "auto", "4", "local"]
    assert full_command(t["to-cng"]) == [
        "python", "/app/ngiab_to_cng.py", "--output", "/cwl-output/cfe-01031510_cng",
        "--configuration-name", "ngiab_cfe_default", "/cwl-inputs/ngen_run/cfe-01031510"]
    assert t["to-cng"]["outputs"]["artifacts"] == [{"name": "cng", "path": "/cwl-output/cfe-01031510_cng"}]


def test_calibration_chain(calibration):
    t = templates(calibration)
    assert full_command(t["calibrate"]) == [
        "/opt/ngiab_calibrate.sh", "/cwl-inputs/ngen_run/cfe-01031510", "/cwl-output/cfe-01031510_calibrated",
        "01031510", "3", "365", "0.5"]
    assert t["calibrate"]["outputs"]["artifacts"] == [
        {"name": "ngen-run", "path": "/cwl-output/cfe-01031510_calibrated"}]
    assert t["run"]["outputs"]["artifacts"] == [{"name": "ngen-run", "path": "/cwl-output/cfe-01031510_calibrated"}]
    assert t["to-cng"]["outputs"]["artifacts"] == [{"name": "cng", "path": "/cwl-output/cfe-01031510_calibrated_cng"}]
    assert "ngiab_cfe_calibrated" in t["to-cng"]["container"]["args"]
