from greenlight.config import Routing
from greenlight.diff import classify


def test_backend_only():
    c = classify(["api/users.py", "services/auth.py"], Routing())
    assert c.label == "backend"
    assert c.backend and not c.frontend


def test_frontend_only():
    c = classify(["src/components/Button.tsx", "web/app.css"], Routing())
    assert c.label == "frontend"
    assert c.frontend and not c.backend


def test_mixed():
    c = classify(["api/users.py", "src/components/Button.tsx"], Routing())
    assert c.label == "mixed"
    assert c.frontend and c.backend


def test_other():
    c = classify(["README.md", "docs/guide.txt"], Routing())
    assert c.label == "other"
    assert not c.frontend and not c.backend


def test_nested_dir_glob():
    c = classify(["frontend/pages/home/index.ts"], Routing())
    assert c.frontend


def test_custom_routing():
    r = Routing(frontend=["ui/**", "ui/*"], backend=["lib/*"])
    c = classify(["ui/x.qml"], r)
    assert c.frontend
