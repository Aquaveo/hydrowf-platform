import os
from pathlib import Path

import pytest
import yaml

EXAMPLES = Path(__file__).parent.parent / "examples"

# ENTRYPOINTs of the hydrowf-tools images by repository (docker image inspect, 2026-11-11:
# preprocess 4.9.5.dev2-02c4145, ciroh-ngen-image v2.1.0 digest, to-cng 0.1.0, calibrate 0.2.0),
# so the tests need neither Docker nor a registry. Keyed by repository so a tag bump in
# hydrowf-tools doesn't break the tests; a new image does, on purpose.
IMAGE_ENTRYPOINTS = {
    "hydrowf/ngiab-preprocess": [],
    "awiciroh/ciroh-ngen-image": ["/ngen/HelloNGEN.sh"],
    "hydrowf/ngiab-to-cng": ["python", "/app/ngiab_to_cng.py"],
    "hydrowf/ngiab-calibrate": ["/opt/ngiab_calibrate.sh"],
}


def repository(image):
    """Image reference without tag or digest (a registry port, host:5000/x, is kept)."""
    name = image.split("@")[0]
    return name.rsplit(":", 1)[0] if ":" in name.rsplit("/", 1)[-1] else name


@pytest.fixture
def entrypoints():
    """Stub for the image ENTRYPOINT lookup; unknown images fail the test loudly."""
    def lookup(image):
        repo = repository(image)
        if repo not in IMAGE_ENTRYPOINTS:
            raise AssertionError(f"test has no ENTRYPOINT for {image}: add it to IMAGE_ENTRYPOINTS")
        return IMAGE_ENTRYPOINTS[repo]
    return lookup


@pytest.fixture
def write_cwl(tmp_path):
    """Write a CWL document (dict) into tmp_path and return its path."""
    def write(name, doc):
        path = tmp_path / name
        path.write_text(yaml.safe_dump(doc, sort_keys=False))
        return path
    return write


@pytest.fixture(scope="session")
def hydrowf_tools():
    """A checkout of Aquaveo/hydrowf-tools, from $HYDROWF_TOOLS (CI checks it out)."""
    path = os.environ.get("HYDROWF_TOOLS")
    if not path:
        pytest.skip("set HYDROWF_TOOLS to a hydrowf-tools checkout to run the integration tests")
    root = Path(path)
    assert (root / "pipelines").is_dir(), f"{root} is not a hydrowf-tools checkout"
    return root


@pytest.fixture(scope="session")
def example_job():
    def load(name):
        return yaml.safe_load((EXAMPLES / name).read_text())
    return load
