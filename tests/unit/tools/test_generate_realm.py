from hacknc.tools import generate_realm


def test_committed_realm_matches_generator() -> None:
    """If this fails, run `make realm` and commit the regenerated JSON."""
    assert generate_realm.OUTPUT.read_text() == generate_realm.render()


def test_no_agent_can_receive_rx_approve() -> None:
    realm = generate_realm.build_realm()
    holders = [c["clientId"] for c in realm["clients"]
               if "rx:approve" in c.get("optionalClientScopes", [])]
    assert set(holders) <= {"hospital-ui", "workshop-cli"}  # human-facing apps only


def test_every_audience_scope_targets_an_existing_client() -> None:
    realm = generate_realm.build_realm()
    client_ids = {c["clientId"] for c in realm["clients"]}
    for target in generate_realm.AUDIENCES:
        assert target in client_ids


def test_service_account_users_exist_for_service_account_clients() -> None:
    realm = generate_realm.build_realm()
    accounts = {u.get("serviceAccountClientId") for u in realm["users"]}
    for client in realm["clients"]:
        if client["serviceAccountsEnabled"]:
            assert client["clientId"] in accounts


def test_every_ward_patient_has_a_portal_login_and_a_landing_entry() -> None:
    from hacknc.ui import presenters

    realm = generate_realm.build_realm()
    patients = {u["username"] for u in realm["users"] if "patient" in u.get("realmRoles", [])}
    accounts = {u for u, *_ in presenters.PORTAL_PATIENTS}
    assert len(patients) == 12 and patients == accounts
    assert presenters.PORTAL_PATIENTS[0][0] == "jane.doe"
