"""Synthetic configuration tests; no DB or API calls."""
import os
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import config
from backend.agent.settings import Settings
from backend.main import app,get_agent

KEYS=('OPENAI_API_KEY','OPENAI_MODEL','AGENT_DATABASE_URL','CITYLENS_DB_PASSWORD','DATABASE_URL')


def make_env(tmp_path,monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key,raising=False)
    monkeypatch.delenv('PYTHON_DOTENV_DISABLED',raising=False)
    root=tmp_path/'fixture-project'
    root.mkdir()
    env=root/'.env'
    env.write_text('OPENAI_API_KEY=synthetic-test-key\nOPENAI_MODEL=fixture-model\n'
        'AGENT_DATABASE_URL=postgresql://reader:synthetic-reader-password@host/db\n'
        'CITYLENS_DB_PASSWORD=synthetic-admin-password\n',encoding='utf-8')
    monkeypatch.setattr(config,'ENV_FILE',env)
    return env


def test_loads_all_supported_values_before_settings(tmp_path,monkeypatch):
    make_env(tmp_path,monkeypatch)
    settings=Settings.from_env()
    assert settings.openai_api_key=='synthetic-test-key'
    assert settings.model=='fixture-model'
    assert settings.database_url=='postgresql://reader:synthetic-reader-password@host/db'
    assert os.environ['CITYLENS_DB_PASSWORD']=='synthetic-admin-password'


def test_file_values_override_process_values_including_empty_process_values(tmp_path,monkeypatch):
    make_env(tmp_path,monkeypatch)
    for key in KEYS[:4]:
        monkeypatch.setenv(key,'process-value')
    settings=Settings.from_env()
    assert settings.openai_api_key=='synthetic-test-key'
    assert settings.model=='fixture-model'
    assert settings.database_url=='postgresql://reader:synthetic-reader-password@host/db'
    assert os.environ['CITYLENS_DB_PASSWORD']=='synthetic-admin-password'
    monkeypatch.setenv('OPENAI_API_KEY','')
    assert Settings.from_env().openai_api_key=='synthetic-test-key'


def test_explicit_empty_file_values_override_process_values(tmp_path,monkeypatch):
    env=make_env(tmp_path,monkeypatch)
    env.write_text('OPENAI_API_KEY=\n',encoding='utf-8')
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-shell-key')
    monkeypatch.setenv('OPENAI_MODEL','shell-model')
    settings=Settings.from_env()
    assert settings.openai_api_key==''
    assert settings.model=='shell-model'  # Keys absent from the file survive.


def test_loading_is_independent_of_cwd(tmp_path,monkeypatch):
    make_env(tmp_path,monkeypatch)
    other=tmp_path/'unrelated'
    other.mkdir()
    (other/'.env').write_text('OPENAI_MODEL=wrong-cwd-model\n',encoding='utf-8')
    monkeypatch.chdir(other)
    assert Settings.from_env().model=='fixture-model'


def test_missing_file_is_ok(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'ENV_FILE',tmp_path/'absent.env')
    monkeypatch.setenv('OPENAI_MODEL','shell-model')
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    assert config.load_environment() is False
    assert Settings.from_env().model=='shell-model'


def test_loader_never_prints_or_logs_secrets(tmp_path,monkeypatch,capsys,caplog):
    make_env(tmp_path,monkeypatch)
    Settings.from_env()
    captured=capsys.readouterr()
    assert captured.out=='' and captured.err=='' and caplog.text==''


def test_fastapi_loads_env_before_settings_without_exposing_secrets(tmp_path,monkeypatch):
    make_env(tmp_path,monkeypatch)
    for key in KEYS[:4]:
        monkeypatch.setenv(key,'stale-shell-value')
    get_agent.cache_clear()
    try:
        with TestClient(app) as client:
            assert os.environ['CITYLENS_DB_PASSWORD']=='synthetic-admin-password'
            assert get_agent.cache_info().currsize==0
            response=client.get('/api/v1/status')
            settings=get_agent().settings
            assert settings.model=='fixture-model'
            assert settings.openai_api_key=='synthetic-test-key'
            assert settings.database_url=='postgresql://reader:synthetic-reader-password@host/db'
        assert response.status_code==200
        assert response.json()['openai_configured'] and response.json()['database_configured']
        assert 'synthetic-' not in response.text and 'postgresql://' not in response.text
    finally:
        get_agent.cache_clear()


def test_fresh_fastapi_processes_use_file_after_restart(tmp_path,monkeypatch):
    """Simulate two server starts inheriting conflicting PowerShell variables."""
    import subprocess
    import sys
    env_file=make_env(tmp_path,monkeypatch)
    root=Path(config.__file__).resolve().parent.parent
    environment={**os.environ,'PYTHONPATH':str(root)}
    environment.update({key:'stale-shell-value' for key in KEYS[:4]})
    script='''
import sys
from pathlib import Path
from backend import config
config.ENV_FILE=Path(sys.argv[1])
from backend.main import app,get_agent
from fastapi.testclient import TestClient
with TestClient(app) as client:
    assert client.get('/api/v1/status').status_code==200
    assert get_agent().settings.model==sys.argv[2]
    assert get_agent().settings.openai_api_key=='synthetic-test-key'
print('startup verified')
'''
    for model in ['fixture-model','updated-file-model']:
        if model=='updated-file-model':
            text=env_file.read_text(encoding='utf-8')
            env_file.write_text(text.replace('fixture-model',model),encoding='utf-8')
        process=subprocess.run([sys.executable,'-c',script,str(env_file),model],
            env=environment,cwd=tmp_path,capture_output=True,text=True,timeout=30)
        assert process.returncode==0
        assert process.stdout.strip()=='startup verified'
        assert 'synthetic-test-key' not in process.stderr
        assert 'synthetic-reader-password' not in process.stderr


def test_standalone_db_cli_loads_env_before_mocked_connect(tmp_path,monkeypatch):
    from backend import db
    env=make_env(tmp_path,monkeypatch)
    with env.open('a',encoding='utf-8') as stream:
        stream.write('DATABASE_URL=postgresql://maintenance:synthetic-password@host/db\n')
    monkeypatch.setattr('sys.argv',['db.py','init'])
    monkeypatch.setenv('DATABASE_URL','stale-shell-value')
    with patch('backend.db.psycopg.connect') as connect,patch('backend.db.initialize'):
        db.main()
        assert connect.call_args.args[0]=='postgresql://maintenance:synthetic-password@host/db'


def test_example_and_gitignore_are_safe():
    root=Path(config.__file__).resolve().parent.parent
    ignore=(root/'.gitignore').read_text(encoding='utf-8').splitlines()
    assert '.env' in ignore and '.env.*' in ignore and '!.env.example' in ignore
    assert ignore.index('!.env.example')>ignore.index('.env.*')
    template=(root/'.env.example').read_text(encoding='utf-8')
    assert 'OPENAI_API_KEY=\n' in template and 'AGENT_DATABASE_URL=\n' in template


def test_verification_cli_cannot_load_developer_dotenv(monkeypatch):
    import subprocess
    root=Path(config.__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(root/'backend'/'verification'))
    import dbtools
    result=subprocess.CompletedProcess([],0,stdout='',stderr='')
    with patch.object(dbtools.subprocess,'run',return_value=result) as run:
        dbtools.run_cli('postgresql://fixture@localhost:54330/fixture_test','db.py','init')
    environment=run.call_args.kwargs['env']
    assert environment['PYTHON_DOTENV_DISABLED']=='1'
    assert environment['DATABASE_URL']=='postgresql://fixture@localhost:54330/fixture_test'
