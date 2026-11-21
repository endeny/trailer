"""Private HF dataset storage. R2 keeps only a small serving manifest and job state."""
import hashlib
import json
import re
from pathlib import Path

REPO_LIMIT_BYTES = 90_000_000_000


class HFStorageError(Exception):
    pass


class HFStore:
    def __init__(self, repo_id, token, api=None, download=None):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo_id or '') or not token:
            raise HFStorageError('hf_not_configured')
        if api is None or download is None:
            from huggingface_hub import HfApi, hf_hub_download
            api = api or HfApi(token=token)
            download = download or hf_hub_download
        self.repo_id, self.token, self.api, self.download = repo_id, token, api, download

    def lookup(self, base):
        try:
            info = self.api.dataset_info(self.repo_id)
            path = self.download(repo_id=self.repo_id, filename=f'{base}/manifest.json', repo_type='dataset',
                                 revision=info.sha, token=self.token)
            state = json.loads(Path(path).read_text())
            if state.get('storage') != 'hf' or not state.get('variants'):
                raise HFStorageError('hf_invalid_manifest')
            state['hf_repo_id'], state['hf_revision'] = self.repo_id, info.sha
            return state
        except HFStorageError:
            raise
        except Exception as error:
            if getattr(getattr(error, 'response', None), 'status_code', None) == 404:
                return None
            raise HFStorageError('hf_lookup_failed') from None

    def ensure_capacity(self, reserve_bytes):
        try:
            total = sum(int(getattr(entry, 'size', 0) or 0) for entry in self.api.list_repo_tree(
                self.repo_id, repo_type='dataset', recursive=True))
        except Exception:
            raise HFStorageError('hf_capacity_lookup_failed') from None
        if total + reserve_bytes > REPO_LIMIT_BYTES:
            raise HFStorageError('hf_storage_budget_exceeded')
        return total

    def upload(self, base, identity, candidate, variants, duration):
        from huggingface_hub import CommitOperationAdd
        reserve = sum(value['file'].stat().st_size for value in variants.values())
        self.ensure_capacity(reserve)
        manifest = {'schema_version': 2, **identity, 'storage': 'hf',
                    'hf_repo_id': self.repo_id, 'youtube_id': candidate.get('youtube_id'),
                    'source': candidate.get('source'), 'title': candidate.get('title'),
                    'duration_seconds': duration, 'variants': {}}
        operations = []
        for quality, variant in variants.items():
            path = variant['file']
            with path.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            remote = f'{base}/{digest}/{quality}.mp4'
            manifest['variants'][quality] = {'path': remote, 'size_bytes': path.stat().st_size,
                                            'width': variant['width'], 'height': variant['height'], 'sha256': digest}
            operations.append(CommitOperationAdd(path_in_repo=remote, path_or_fileobj=str(path)))
        operations.append(CommitOperationAdd(path_in_repo=f'{base}/manifest.json',
                                             path_or_fileobj=json.dumps(manifest, ensure_ascii=False).encode()))
        try:
            commit = self.api.create_commit(repo_id=self.repo_id, repo_type='dataset', operations=operations,
                                             commit_message=f"Cache trailer {identity['media_type']}/{identity['tmdb_id']}/{identity['language']}")
        except Exception:
            raise HFStorageError('hf_upload_failed') from None
        manifest['hf_revision'] = commit.oid
        return manifest
