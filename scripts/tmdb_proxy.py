"""Headers for the existing iPlay software-attestation metadata API."""
import hashlib
import time
import uuid

def headers(path):
    nonce, stamp = str(uuid.uuid4()), str(int(time.time()*1000))
    trace = hashlib.sha256(f'GET:{path}:nonce={nonce}:time={stamp}:body=none'.encode()).hexdigest()
    return {'User-Agent':'iPlay/1.0', 'X-iPlay-Client-Platform':'ios', 'X-iPlay-Client-Version':'1.0',
            'X-iPlay-Client-Request-ID':nonce, 'X-iPlay-Client-Timestamp':stamp,
            'X-iPlay-Attest-Mode':'simulated', 'X-iPlay-Attest-Trace':trace}
