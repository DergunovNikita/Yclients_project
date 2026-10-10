from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
# alembic_version.version_num is VARCHAR(32): a longer revision id fails only when deploy runs `migrate` on production.
MAX_REVISION_ID_LENGTH = 32


def _script_directory() -> ScriptDirectory:
    cfg = Config(str(ROOT / 'alembic.ini'))
    cfg.set_main_option('script_location', str(ROOT / 'alembic'))
    return ScriptDirectory.from_config(cfg)


def test_chain_has_a_single_head():
    assert len(_script_directory().get_heads()) == 1


def test_revision_ids_fit_alembic_version_column():
    too_long = [
        script.revision
        for script in _script_directory().walk_revisions()
        if len(script.revision) > MAX_REVISION_ID_LENGTH
    ]
    assert too_long == []


def test_every_revision_defines_downgrade():
    missing = [
        script.revision
        for script in _script_directory().walk_revisions()
        if not callable(getattr(script.module, 'downgrade', None))
    ]
    assert missing == []
