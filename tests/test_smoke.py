import ccfleet_agent
import ccfleetd


def test_packages_import():
    assert ccfleetd.__version__ == ccfleet_agent.__version__
