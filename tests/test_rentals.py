"""Direct rentals must stay on the chosen private node and require authorization."""
import copy

import httpx
import pytest
from fastapi.testclient import TestClient

from ext.fabric_client import FabricClient
from ext.main import create_app
from test_ext_approval import CAPACITY


@pytest.fixture
def rental_client():
    capacity = copy.deepcopy(CAPACITY)
    node = capacity['nodes'][1]
    node['labels'].update(source_type='personal_private', owner_user_id='provider',
                          fabric_node_id=node['node_id'], hourly_credit='2')
    sent = []
    def handle(request):
        if request.url.path == '/v1/capacity':
            return httpx.Response(200, json=capacity)
        if request.url.path == '/v1/allocations':
            import json
            sent.append(json.loads(request.content))
            return httpx.Response(201, json={'id': 'alloc_rental', 'state': 'Allocated'})
        return httpx.Response(404)
    with TestClient(create_app(FabricClient(base_url='http://test', transport=httpx.MockTransport(handle)))) as client:
        yield client, sent


def rent(client, **extra):
    return client.post('/ext/v1/rentals', headers={'X-Owner': 'borrower'},
                       json={'node_id': 'lab-a100-01', 'duration_seconds': 1800, **extra})


def test_rental_approval_then_exact_node_submission(rental_client):
    client, sent = rental_client
    result = rent(client, gpu_count=2)
    assert result.status_code == 201, result.text
    decision, approval = result.json()['decision'], result.json()['approval']
    assert decision['request_kind'] == 'private_rental'
    assert decision['estimated_cost'] == 2
    assert sent == []  # applying does not hold GPUs
    path = f"/ext/v1/decisions/{decision['decision_id']}/submit"
    assert client.post(path, json={}, headers={'X-Owner': 'borrower'}).status_code == 409
    approve = f"/ext/v1/approvals/{approval['approval_id']}/decision"
    assert client.post(approve, json={'action': 'accept'}, headers={'X-Owner': 'stranger'}).status_code == 403
    assert client.post(approve, json={'action': 'accept'}, headers={'X-Owner': 'provider'}).status_code == 200
    assert client.post(path, json={}, headers={'X-Owner': 'borrower'}).status_code == 201
    assert sent[0]['node_selector'] == {'fabric_node_id': 'lab-a100-01'}
    assert sent[0]['ttl_seconds'] == 1800
    assert sent[0]['request']['gpu']['count'] == 2
    assert client.post(path, json={}, headers={'X-Owner': 'borrower'}).status_code == 201
    assert len(sent) == 1
    assert client.get('/ext/v1/rentals', headers={'X-Owner': 'stranger'}).json()['count'] == 0
    assert client.get('/ext/v1/rentals', headers={'X-Owner': 'provider'}).json()['count'] == 1


@pytest.mark.parametrize('extra,status', [({'node_id': 'central-01'},422),
    ({'node_id': 'missing'},404), ({'gpu_count': 3},422),
    ({'duration_seconds': 7200},422), ({'duration_seconds': 0},422)])
def test_invalid_rental_does_not_allocate(rental_client, extra, status):
    client, sent = rental_client
    assert rent(client, **extra).status_code == status
    assert sent == []


def test_rental_can_be_cancelled(rental_client):
    client, _ = rental_client
    approval = rent(client).json()['approval']
    path = f"/ext/v1/approvals/{approval['approval_id']}/cancel"
    assert client.post(path, headers={'X-Owner':'stranger'}).status_code == 403
    assert client.post(path, headers={'X-Owner':'borrower'}).status_code == 200
    record = client.get('/ext/v1/rentals', headers={'X-Owner':'borrower'}).json()['items'][0]
    assert record['approval']['status'] == 'cancelled'


def test_busy_card_keeps_authorization_without_allocating(rental_client, monkeypatch):
    client, sent = rental_client
    result = rent(client).json()
    approval, decision = result['approval'], result['decision']
    client.post(f"/ext/v1/approvals/{approval['approval_id']}/decision",
                json={'action':'accept'}, headers={'X-Owner':'provider'})
    capacity = client.app.state.fabric.capacity()
    for gpu in capacity.nodes[1].gpus:
        gpu.held_by = 'contributor'
    monkeypatch.setattr(client.app.state.fabric, 'capacity', lambda: capacity)
    response = client.post(f"/ext/v1/decisions/{decision['decision_id']}/submit",
                           json={}, headers={'X-Owner':'borrower'})
    assert response.status_code == 409
    assert response.json()['error'] == 'rental_busy'
    assert sent == []
    assert client.get('/ext/v1/rentals', headers={'X-Owner':'borrower'}).json()['items'][0]['approval']['status'] == 'accepted'
