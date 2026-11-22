"""Bounded, one-shot native Arr requests for whole-title cleanup."""
import requests


class TitleCleanupTransport:
    def cleanup_request(self, method, resource, *, params=None, payload=None):
        # Resource names come exclusively from cleanup code, never user URLs.
        try:
            response = self.connect.session.request(
                method, f'{self.url}/api/v3/{resource}',
                headers={'X-Api-Key': self.apikey}, params=params, json=payload,
                timeout=(5, 20), allow_redirects=False)
            data = None
            if response.content:
                try:
                    data = response.json()
                except ValueError:
                    return {'status_code': response.status_code, 'error': 'invalid_response'}
            return {'status_code': response.status_code, 'data': data}
        except requests.RequestException:
            return {'error': 'request_uncertain'}
