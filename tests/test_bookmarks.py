import json
from pathlib import Path
from unittest.mock import Mock
import httpx
import pytest
from x2o.bookmarks import XBookmarksAPI, export_api, export_urls, load_export, cleanup, sync
from x2o.models import Config, Post, Research, Topic, Source
from x2o.vault import plan, apply_plan


def archive(vault, post_id):
    post = Post(id=post_id, url=f'https://x.com/a/status/{post_id}', author='a', author_name='A',
                published='2024-01-01', text='post')
    research = Research(title='Research', summary='Summary', findings='Findings', opinions='None', limitations='None',
        media_analysis='None', topics=[Topic(name='Shared topic', overview='Intro', contribution='Evidence', related=[])],
        sources=[Source(title='Evidence', url=post.url, kind='primary')])
    return plan(vault, post, research, 'test', 'test', [])


def fake_api():
    api = Mock()
    api.identity.return_value = {'id':'7','username':'alice'}
    api.list_bookmarks.return_value = ['https://x.com/a/status/123','https://x.com/b/status/456']
    return api


def test_paginated_export(tmp_path):
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.path == '/2/users/me':
            return httpx.Response(200,json={'data':{'id':'7','username':'alice'}})
        if not request.url.params.get('pagination_token'):
            return httpx.Response(200,json={'data':[{'id':'123'}],'meta':{'next_token':'next'}})
        return httpx.Response(200,json={'data':[{'id':'123'},{'id':'456'}],'meta':{}})
    api = XBookmarksAPI('test-token',transport=httpx.MockTransport(handle))
    file=tmp_path/'bookmarks.txt'
    manifest=export_api(api,file)
    assert [entry['id'] for entry in manifest['bookmarks']] == ['123','456']
    assert load_export(file)['account']['id'] == '7'
    assert calls[-1].url.params['pagination_token'] == 'next'
    assert 'test-token' not in file.read_text() + (tmp_path/'bookmarks.txt.json').read_text()
    api.close()


def test_preview_never_removes_failed_or_dry_run_imports(tmp_path):
    file=tmp_path/'bookmarks.txt'
    export_api(fake_api(),file)
    apply_plan(archive(tmp_path,'123'),tmp_path)
    archive(tmp_path,'456') # Plan exists only in memory; no successful vault commit.
    api=fake_api()
    report=cleanup(file,tmp_path,api)
    assert [x['id'] for x in report['eligible']] == ['123']
    assert [x['id'] for x in report['retained']] == ['456']
    api.remove.assert_not_called()


def test_execute_only_verified_imports_and_correct_account(tmp_path):
    file=tmp_path/'bookmarks.txt'
    api=fake_api()
    export_api(api,file)
    apply_plan(archive(tmp_path,'123'),tmp_path)
    api.identity.return_value={'id':'8','username':'wrong'}
    with pytest.raises(ValueError,match='differs'):
        cleanup(file,tmp_path,api,execute=True)
    api.remove.assert_not_called()
    api.identity.return_value={'id':'7','username':'alice'}
    report=cleanup(file,tmp_path,api,execute=True)
    api.remove.assert_called_once_with('7','123')
    assert len(report['removed']) == 1
    assert '456' in [x['id'] for x in report['retained']]
    assert (tmp_path/'bookmarks.txt.removed.jsonl').exists()


def test_changed_export_or_post_refuses_cleanup(tmp_path):
    file=tmp_path/'bookmarks.txt'
    api=fake_api()
    export_api(api,file)
    apply_plan(archive(tmp_path,'123'),tmp_path)
    note=tmp_path/'Posts/123.md'
    note.write_text('corrupted')
    report=cleanup(file,tmp_path)
    assert not report['eligible']
    file.write_text(file.read_text()+'https://x.com/a/status/789\n')
    with pytest.raises(ValueError,match='changed'):
        cleanup(file,tmp_path,api,execute=True)
    api.remove.assert_not_called()


def test_shared_topic_gaining_more_posts_keeps_receipt_valid(tmp_path):
    file=tmp_path/'bookmarks.txt'
    export_api(fake_api(),file)
    apply_plan(archive(tmp_path,'123'),tmp_path)
    apply_plan(archive(tmp_path,'456'),tmp_path)
    assert len(cleanup(file,tmp_path)['eligible']) == 2
    shared=tmp_path/'Topics/Shared topic.md'
    shared.write_text(shared.read_text().replace('<!-- x2o:123:start -->',''))
    assert [x['id'] for x in cleanup(file,tmp_path)['eligible']] == ['456']


def test_sync_retains_failed_import_and_defaults_to_no_removal(tmp_path):
    api=fake_api()
    file=tmp_path/'bookmarks.txt'
    def ingest(url,cfg,progress):
        if url.endswith('456'):
            raise ValueError('LLM failed')
        return archive(cfg.vault,'123')
    report=sync(api,Config(vault=tmp_path),file,ingest)
    assert report['imports_failed'][0]['id'] == '456'
    api.remove.assert_not_called()
    file2=tmp_path/'second.txt'
    report=sync(api,Config(vault=tmp_path),file2,ingest,remove=True)
    api.remove.assert_called_once_with('7','123')
    assert report['imports_failed'][0]['id'] == '456'


def test_removal_failure_stops_and_keeps_attempt_journal(tmp_path):
    api=fake_api()
    file=tmp_path/'bookmarks.txt'
    export_api(api,file)
    for post_id in ['123','456']:
        apply_plan(archive(tmp_path,post_id),tmp_path)
    api.remove.side_effect=ValueError('X rate limit')
    report=cleanup(file,tmp_path,api,execute=True)
    assert len(report['failures']) == 1
    assert len(report['retained']) == 2
    assert api.remove.call_count == 1
    assert 'attempted' in (tmp_path/'bookmarks.txt.removed.jsonl').read_text()


def test_browser_capture_and_legacy_receipt(tmp_path):
    file=tmp_path/'browser.txt'
    export_urls(['https://x.com/a/status/123','https://twitter.com/a/status/123?s=20'],file,{'username':'alice'},'browser')
    assert len(load_export(file)['bookmarks']) == 1
    apply_plan(archive(tmp_path,'123'),tmp_path)
    api=fake_api()
    cleanup(file,tmp_path,api,execute=True)
    api.remove.assert_called_once_with('7','123')
    state=tmp_path/'.x2o/processed.json'
    data=json.loads(state.read_text());data['123'].pop('post_sha256');state.write_text(json.dumps(data))
    assert not cleanup(file,tmp_path)['eligible']


def test_partial_export_errors_never_replace_existing_files(tmp_path):
    api=fake_api()
    file=tmp_path/'bookmarks.txt'
    export_api(api,file)
    original=file.read_bytes()
    api.list_bookmarks.side_effect=ValueError('partial listing')
    with pytest.raises(ValueError):
        export_api(api,file,overwrite=True)
    assert file.read_bytes() == original


def test_delete_requires_explicit_confirmation_from_x():
    api=XBookmarksAPI('test',transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'data':{'bookmarked':True}})))
    with pytest.raises(ValueError,match='did not confirm'):
        api.remove('7','123')
    api.close()


def test_failed_current_sync_keeps_even_previous_success(tmp_path):
    api=fake_api()
    file=tmp_path/'bookmarks.txt'
    apply_plan(archive(tmp_path,'123'),tmp_path)
    def failed(*args,**kwargs):
        raise ValueError('current import failed')
    report=sync(api,Config(vault=tmp_path),file,failed,remove=True)
    api.remove.assert_not_called()
    assert len(report['cleanup']['retained']) == 2


def test_cli_browser_export_needs_no_api_and_cleanup_previews(tmp_path, monkeypatch):
    from x2o.cli import main
    monkeypatch.delenv('X_USER_ACCESS_TOKEN',raising=False)
    config=tmp_path/'config.toml';config.write_text(f'vault = "{tmp_path}"')
    captured=tmp_path/'captured.txt';captured.write_text('https://x.com/a/status/123\n')
    output=tmp_path/'bookmarks.txt'
    assert main(['--config',str(config),'bookmarks','export','--from-file',str(captured),
        '--account-handle','alice','--output',str(output)]) == 0
    assert main(['--config',str(config),'bookmarks','cleanup','--file',str(output)]) == 0
    assert not json.loads(Path(str(output)+'.cleanup.json').read_text())['eligible']
