from copy import deepcopy
from datetime import date
import unittest
from unittest.mock import patch, MagicMock
import urllib.error

from bookpiano import core
from bookpiano.source import learn_profile, query_data, normalize, Capture

DAY = date(2026, 9, 25)
PATH = "/api/SystemApi/GetMeetingsByRoomAndDateAndUser"
BODY = {"MessageType": 2001, "Data": {"StartDate": "2026-09-25T00:00:00.000Z", "EndDate": "2026-09-25T23:59:59.000Z", "search": "致诚大会议室", "offset": 0, "limit": 20}}
MEETING = {"MeetingID": "m1", "StartDateTime": "2026-09-25T09:00:00", "EndDateTime": "2026-09-25T10:00:00", "Topical": "学习小组", "State": 9}
PAYLOAD = {"IsSuccess": True, "Data": {"rows": [{"MeetingRoomID": "room1", "MeetingRoomName": "致诚大会议室", "Meetings": [MEETING]}], "total": 1}}


class SourceProfileTests(unittest.TestCase):
    def test_learns_nested_room_and_rebinds_only_date(self):
        profile = learn_profile(PATH, BODY, PAYLOAD, "room1", "致诚大会议室", DAY)
        data = query_data(profile, date(2026, 9, 26))
        self.assertEqual(data["StartDate"], "2026-09-26T00:00:00.000Z")
        self.assertEqual(data["EndDate"], "2026-09-26T23:59:59.000Z")
        self.assertEqual(data["search"], "致诚大会议室")
        self.assertEqual(BODY["Data"]["StartDate"], "2026-09-25T00:00:00.000Z")
        normalized = normalize(PAYLOAD, {"source_profile": profile, "room_id": "room1"})
        self.assertEqual(normalized["Data"]["rows"][0]["MeetingRoomID"], "room1")

    def test_missing_room_is_not_empty_schedule(self):
        profile = learn_profile(PATH, BODY, PAYLOAD, "room1", "致诚大会议室", DAY)
        with self.assertRaises(core.FetchError):
            normalize({"IsSuccess": True, "Data": {"rows": []}}, {"source_profile": profile, "room_id": "room1"})

    def test_nested_list_cannot_disguise_another_room(self):
        profile = learn_profile(PATH, BODY, PAYLOAD, "room1", "致诚大会议室", DAY)
        payload = deepcopy(PAYLOAD)
        payload["Data"]["rows"][0]["Meetings"][0]["MeetingRoomID"] = "other-room"
        with self.assertRaises(core.FetchError):
            normalize(payload, {"source_profile": profile, "room_id": "room1"})

    def test_mixed_dates_cannot_save_a_partially_rebound_query(self):
        body = deepcopy(BODY)
        body["Data"]["EndDate"] = "2026-09-26T23:59:59Z"
        with self.assertRaises(core.LocalError):
            learn_profile(PATH, body, PAYLOAD, "room1", "致诚大会议室", DAY)

    def test_empty_known_child_is_allowed_only_under_verified_room(self):
        payload = deepcopy(PAYLOAD)
        payload["Data"]["rows"][0]["Meetings"] = []
        profile = learn_profile(PATH, BODY, payload, "room1", "致诚大会议室", DAY)
        self.assertEqual(normalize(payload, {"source_profile": profile, "room_id": "room1"})["Data"]["rows"], [])

    def test_unknown_parameter_or_mutation_or_wrong_date_is_rejected(self):
        for path, body in [("/api/SystemApi/AddMeeting", BODY),
                           (PATH, {"MessageType": 2001, "Data": dict(BODY["Data"], password="must-not-be-saved")}),
                           (PATH, {"MessageType": 2001, "Data": {"StartDate": "2026-09-24T00:00:00.000Z"}})]:
            with self.subTest(path=path), self.assertRaises(core.LocalError):
                learn_profile(path, body, PAYLOAD, "room1", "致诚大会议室", DAY)

    def test_no_profile_does_not_make_any_request(self):
        with patch("bookpiano.core.urllib.request.build_opener") as opener:
            with self.assertRaises(core.LocalError):
                core.query_bookings(core.default_config(), DAY, "fake")
            opener.assert_not_called()

    def test_404_never_falls_back_or_retries_other_endpoints(self):
        profile = learn_profile(PATH, BODY, PAYLOAD, "room1", "致诚大会议室", DAY)
        config = dict(core.default_config(), room_id="room1", source_profile=profile)
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(core.ORIGIN + PATH, 404, "missing", {}, None)
        with patch("bookpiano.core.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(core.FetchError) as result:
                core.query_bookings(config, DAY, "fake")
        self.assertEqual(result.exception.kind, "endpoint")
        opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
