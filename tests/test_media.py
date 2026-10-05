from pathlib import Path
from unittest.mock import patch
import pytest
from x2o.media import process_media
from x2o.models import Config, Post


def post(media):
    return Post(id='123', url='https://x.com/a/status/123', author='a', author_name='A',
                published='2024-01-01', text='video', media=media)


def test_audio_failure_preserves_frames_and_reports_truncation(tmp_path):
    cfg = Config(vault=tmp_path, max_frames=3, max_video_seconds=10)
    cache = tmp_path/'cache'
    def fake_run(args):
        Path(args[-1]).write_bytes(b'frame or audio')
    with patch('x2o.media.download', return_value=(b'video', 'video/mp4', 'url')), \
         patch('x2o.media.imageio_ffmpeg.read_frames', return_value=(x for x in [{'duration': 60}])), \
         patch('x2o.media.run', side_effect=fake_run), \
         patch('x2o.media.transcribe', side_effect=ValueError('Missing credential')):
        records, images = process_media(post([{'type': 'video', 'url': 'https://example.org/video.mp4'}]), cfg, cache)
    assert len(images) == 3
    assert records[0]['truncated']
    assert records[0]['analyzed_seconds'] == 10
    assert records[0]['audio_status'] == 'failed'
    assert 'Missing credential' in records[0]['audio_error']


def test_no_speech_is_distinct_from_failure(tmp_path):
    cfg = Config(vault=tmp_path, max_frames=1)
    def fake_run(args):
        Path(args[-1]).write_bytes(b'frame or audio')
    with patch('x2o.media.download', return_value=(b'video', 'video/mp4', 'url')), \
         patch('x2o.media.imageio_ffmpeg.read_frames', return_value=(x for x in [{'duration': 60}])), \
         patch('x2o.media.run', side_effect=fake_run), \
         patch('x2o.media.transcribe', return_value=''):
        records, _ = process_media(post([{'type': 'video', 'url': 'https://example.org/video.mp4'}]), cfg, tmp_path/'cache')
    assert records[0]['audio_status'] == 'no_speech_recognized'
    assert 'audio_error' not in records[0]
