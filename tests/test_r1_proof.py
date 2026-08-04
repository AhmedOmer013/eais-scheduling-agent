"""R1-proof test (T11) -- the comprehensive, self-maintaining version of T6's
`TestCoreHasNoSectorNames` in `tests/test_core_orchestration.py`.

T6's check hardcodes the literal strings "clinic"/"restaurant". Two gaps in
that approach:

1. It's not self-maintaining -- a third sector added later wouldn't be
   covered until someone remembered to update the hardcoded list.
2. It only catches literal name strings, not hidden coupling -- code could
   reference a concrete skill pack class (directly, or transitively via
   some other module `core/` imports) without ever spelling out a sector
   name in a string.

This file builds two independent, standalone checks that close both gaps.
Naming sectors here (to build the discovery/import lists) is legal -- the
R1 rule is about `core/`'s own source, not the tests that verify it.

1. `TestDynamicSectorNameScan` -- discovers sector identifiers at test-run
   time from the filesystem (skillpacks/ subpackages + manifest `sector:`
   fields via the real `SectorManifest.load`), instead of a hardcoded
   list, then greps `core/*.py` for any of them. Adding a third sector
   automatically extends its coverage with zero changes to this file.

2. `TestCoreImportGraphNeverLoadsAConcreteSkillPack` -- the required,
   primary check per the task brief. Imports each `core/` module in a
   fresh subprocess (so nothing is pre-imported) and inspects
   `sys.modules` afterwards for any concrete skill-pack subpackage. This
   is the stronger guarantee because it catches *transitive* imports too
   (a module core/ imports pulling in a concrete pack indirectly), not
   just direct `import`/`from` statements in core/*.py itself. A bonus
   static AST check (`TestCoreImportGraphStatic`) is included alongside
   it: faster, no subprocess needed, but only catches direct imports --
   it complements rather than replaces the subprocess check.
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

import eais_scheduling_agent
from eais_scheduling_agent.manifests.manifest import SectorManifest

PACKAGE_ROOT = Path(eais_scheduling_agent.__file__).parent
SKILLPACKS_DIR = PACKAGE_ROOT / "skillpacks"
MANIFESTS_DIR = PACKAGE_ROOT / "manifests"
CORE_DIR = PACKAGE_ROOT / "core"


# ---------------------------------------------------------------------------
# Discovery helpers -- shared by both checks below. Nothing here is a
# hardcoded sector list; everything is derived from the filesystem/manifests
# at test-collection time.
# ---------------------------------------------------------------------------


def _discover_concrete_skillpack_subpackages():
    """Subdirectory names under skillpacks/, excluding __pycache__.

    `base.py` and `__init__.py` are files, not directories, so `is_dir()`
    already excludes them without needing an explicit name check.
    """
    return sorted(
        entry.name
        for entry in SKILLPACKS_DIR.iterdir()
        if entry.is_dir() and entry.name != "__pycache__"
    )


def _discover_manifest_sector_names():
    """The `sector:` field of every manifest file under manifests/.

    Uses the real `SectorManifest.load` (T3) rather than hand-rolling
    YAML/JSON parsing, so this stays correct if the manifest format
    changes.
    """
    names = set()
    for pattern in ("*.yaml", "*.yml", "*.json"):
        for manifest_path in MANIFESTS_DIR.glob(pattern):
            manifest = SectorManifest.load(str(manifest_path))
            names.add(manifest.sector)
    return names


def _discover_sector_names():
    """Union of skillpacks/ subpackage names and manifest sector: fields."""
    return set(_discover_concrete_skillpack_subpackages()) | _discover_manifest_sector_names()


def _discover_core_modules():
    """core/*.py files, sorted, for scanning/importing."""
    return sorted(CORE_DIR.glob("*.py"))


def _discover_core_module_names():
    """Dotted import names for every core/*.py file (excluding __init__)."""
    return sorted(
        f"eais_scheduling_agent.core.{module.stem}"
        for module in _discover_core_modules()
        if module.stem != "__init__"
    )


# ---------------------------------------------------------------------------
# Check 1: dynamic, self-maintaining sector-name text scan.
# ---------------------------------------------------------------------------


class TestDynamicSectorNameScan:
    """Generalizes T6's hardcoded grep: discovers sector names at run time.

    If a third sector (e.g. "salon") is added, this test automatically
    starts scanning core/ for "salon" too -- no edits to this file needed.
    """

    def test_at_least_two_sectors_are_discovered(self):
        """Can't prove anything if discovery itself silently finds nothing.

        Same "can't pass vacuously" discipline as T6's `assert modules`.
        """
        sector_names = _discover_sector_names()

        assert len(sector_names) >= 2, (
            "sector discovery found fewer than 2 sectors "
            f"({sector_names!r}) -- discovery logic may be broken, which "
            "would let this test pass vacuously"
        )

    def test_no_discovered_sector_name_appears_in_core_source(self):
        sector_names = _discover_sector_names()
        assert len(sector_names) >= 2, (
            f"sector discovery found fewer than 2 sectors ({sector_names!r})"
        )

        pattern = re.compile(
            "|".join(re.escape(name) for name in sorted(sector_names)),
            re.IGNORECASE,
        )

        core_modules = _discover_core_modules()
        assert core_modules, f"no core modules found to scan in {CORE_DIR}"

        offenders = {}
        for module in core_modules:
            hits = [
                f"{module.name}:{number}: {line.strip()}"
                for number, line in enumerate(
                    module.read_text(encoding="utf-8").splitlines(), start=1
                )
                if pattern.search(line)
            ]
            if hits:
                offenders[module.name] = hits

        assert offenders == {}, (
            f"discovered sector name(s) {sorted(sector_names)!r} found in "
            f"core/: {offenders}"
        )


# ---------------------------------------------------------------------------
# Check 2 (required, primary): import-graph check via a fresh subprocess.
#
# Proves no core/ module transitively imports a concrete skill pack, by
# actually importing it in a clean interpreter (nothing pre-imported) and
# inspecting sys.modules afterwards -- this catches indirect imports (via
# some other module core/ imports) that a static source scan of core/*.py
# alone would miss.
# ---------------------------------------------------------------------------

_SUBPROCESS_SCRIPT = """
import sys
import {module}
leaked = sorted(
    name for name in sys.modules
    if name.startswith("eais_scheduling_agent.skillpacks.")
    and name.split(".")[2] != "base"
)
print(repr(leaked))
"""


class TestCoreImportGraphNeverLoadsAConcreteSkillPack:
    """Required primary check per the T11 brief: subprocess-based, catches
    transitive imports, not just direct ones in core/*.py.
    """

    def test_at_least_two_concrete_skill_packs_exist_to_check_against(self):
        """Can't prove anything if there's nothing concrete to leak."""
        sector_dirs = _discover_concrete_skillpack_subpackages()

        assert len(sector_dirs) >= 2, (
            "fewer than 2 concrete skillpacks/ subpackages found "
            f"({sector_dirs!r}) -- this check would pass vacuously"
        )

    def test_importing_each_core_module_in_isolation_never_pulls_in_a_concrete_pack(
        self,
    ):
        sector_dirs = _discover_concrete_skillpack_subpackages()
        assert len(sector_dirs) >= 2, (
            f"fewer than 2 concrete skillpacks/ subpackages found ({sector_dirs!r})"
        )

        core_module_names = _discover_core_module_names()
        assert core_module_names, f"no core modules found to import in {CORE_DIR}"

        for module_name in core_module_names:
            script = _SUBPROCESS_SCRIPT.format(module=module_name)
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=30,
            )

            assert result.returncode == 0, (
                f"subprocess import of {module_name} failed "
                f"(exit {result.returncode}):\n{result.stderr}"
            )

            leaked = ast.literal_eval(result.stdout.strip())
            assert leaked == [], (
                f"importing {module_name} in a clean interpreter pulled in "
                f"concrete skill pack module(s) via sys.modules: {leaked}"
            )


# ---------------------------------------------------------------------------
# Check 2b (bonus/optional, complementary): static AST check.
#
# Faster and doesn't need a subprocess, but only catches *direct*
# import/from statements in core/*.py itself -- it would miss a concrete
# pack pulled in transitively through some other module core/ imports.
# The subprocess check above is the one that closes that gap; this one is
# just a quick, cheap first line of defense alongside it.
# ---------------------------------------------------------------------------


def _concrete_skillpack_import_target(dotted_name, sector_dirs):
    """Return the offending dotted name if it names a concrete skill pack
    subpackage (or something inside one), else None.
    """
    prefix = "eais_scheduling_agent.skillpacks."
    if not dotted_name.startswith(prefix):
        return None
    remainder = dotted_name[len(prefix):].split(".")[0]
    if remainder in sector_dirs:
        return dotted_name
    return None


class TestCoreImportGraphStatic:
    """Bonus complementary check: no core/*.py file directly imports a
    concrete skill pack subpackage.
    """

    def test_no_direct_ast_import_of_a_concrete_skill_pack(self):
        sector_dirs = _discover_concrete_skillpack_subpackages()
        assert len(sector_dirs) >= 2, (
            f"fewer than 2 concrete skillpacks/ subpackages found ({sector_dirs!r})"
        )

        core_modules = _discover_core_modules()
        assert core_modules, f"no core modules found to scan in {CORE_DIR}"

        offenders = {}
        for module in core_modules:
            tree = ast.parse(
                module.read_text(encoding="utf-8"), filename=str(module)
            )
            hits = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        target = _concrete_skillpack_import_target(
                            alias.name, sector_dirs
                        )
                        if target:
                            hits.append(f"import {target}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    target = _concrete_skillpack_import_target(
                        node.module, sector_dirs
                    )
                    if target:
                        hits.append(f"from {target} import ...")
            if hits:
                offenders[module.name] = hits

        assert offenders == {}, (
            f"direct import of a concrete skill pack found in core/: {offenders}"
        )
