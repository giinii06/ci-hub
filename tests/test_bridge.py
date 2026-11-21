import unittest
from unittest.mock import patch

import bridge


class PublicationTests(unittest.TestCase):
    def test_unapproved_tag_blocks_all_remote_writes(self):
        config = {"github_owner": "example"}
        entry = {"id": 1, "sha": "a" * 40, "stack": "go"}
        calls = []

        def github(endpoint, **options):
            calls.append(("github", endpoint, options.get("method", "GET")))
            return None

        def gitlab(endpoint, **options):
            calls.append(("gitlab", endpoint, options.get("method", "GET")))
            if endpoint.endswith("/remote_mirrors?per_page=100"):
                return []
            if endpoint.endswith("/protected_branches?per_page=100"):
                return [{"name": "main"}]
            if endpoint.endswith("/repository/branches/main"):
                return {"commit": {"id": entry["sha"]}}
            if "/repository/files/" in endpoint:
                return {"content": "Y2FsbGVy"}
            self.fail(f"Unexpected call: {endpoint}")

        with patch.object(bridge, "github", github), patch.object(bridge, "gitlab", gitlab), \
                patch.object(bridge, "caller", return_value="caller"), \
                patch.object(bridge, "tags", return_value={"release": "b" * 40}):
            with self.assertRaisesRegex(ValueError, "tags differ"):
                bridge.provision(config, entry, {"id": 1}, "example/mirror")

        self.assertTrue(calls)
        self.assertEqual({method for _, _, method in calls}, {"GET"})
