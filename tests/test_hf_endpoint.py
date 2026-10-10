"""Offline checks for HF endpoint selection and SDK forwarding."""

import os
import unittest
from unittest import mock

import download
import huggingface_hub  # Import first: endpoint selection must survive cached SDK defaults.


class HFEndpointTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_explicit_endpoint_overrides_env_without_probe(self):
        os.environ["HF_ENDPOINT"] = "https://env.example"
        with mock.patch.object(download, "_is_endpoint_reachable") as probe:
            endpoint = download._configure_hf_endpoint_or_exit("https://cli.example/")
        self.assertEqual(endpoint, "https://cli.example")
        self.assertEqual(os.environ["HF_ENDPOINT"], endpoint)
        probe.assert_not_called()

    def test_env_endpoint_is_forced_without_probe(self):
        os.environ["HF_ENDPOINT"] = "https://env.example"
        with mock.patch.object(download, "_is_endpoint_reachable") as probe:
            self.assertEqual(
                download._configure_hf_endpoint_or_exit(), "https://env.example"
            )
        probe.assert_not_called()

    def test_auto_selection(self):
        for responses, expected in (
            ([True], download.HF_MIRROR_ENDPOINT),
            ([False, True], download.HF_PRIMARY_ENDPOINT),
        ):
            with self.subTest(expected=expected), mock.patch.dict(
                os.environ, {}, clear=True
            ), mock.patch.object(
                download, "_is_endpoint_reachable", side_effect=responses
            ) as probe:
                self.assertEqual(download._configure_hf_endpoint_or_exit(), expected)
                self.assertEqual(os.environ["HF_ENDPOINT"], expected)
                self.assertEqual(probe.call_args_list, [
                    mock.call(endpoint) for endpoint in (
                        download.HF_MIRROR_ENDPOINT, download.HF_PRIMARY_ENDPOINT
                    )[:len(responses)]
                ])

    def test_no_reachable_endpoint_fails(self):
        with mock.patch.object(download, "_is_endpoint_reachable", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "均不可达"):
                download._configure_hf_endpoint_or_exit()

    def test_download_forwards_endpoint_to_preimported_sdk(self):
        os.environ["HF_ENDPOINT"] = "https://env.example"
        argv = ["download.py", "download", "org/model", "--source",
                "hf-mirror", "--local-dir", "/tmp/model"]
        with mock.patch("sys.argv", argv), mock.patch.object(
            huggingface_hub, "snapshot_download"
        ) as snapshot, mock.patch.object(download, "_get_token", return_value=None):
            self.assertEqual(download.main(), 0)
        self.assertEqual(snapshot.call_args.kwargs["endpoint"], "https://hf-mirror.com")
        self.assertEqual(snapshot.call_args.kwargs["max_workers"], 8)
        self.assertEqual(snapshot.call_args.kwargs["local_dir"], "/tmp/model")

    def test_forced_download_failure_does_not_fallback(self):
        argv = ["download.py", "download", "org/model", "--source", "hf-mirror"]
        with mock.patch("sys.argv", argv), mock.patch.object(
            huggingface_hub, "snapshot_download", side_effect=RuntimeError("unreachable")
        ) as snapshot, mock.patch.object(
            download, "_is_endpoint_reachable"
        ) as probe, mock.patch.object(download, "_get_token", return_value=None):
            self.assertEqual(download.main(), 1)
        snapshot.assert_called_once()
        probe.assert_not_called()

    def test_search_forwards_endpoint(self):
        argv = ["download.py", "search", "Qwen", "--source", "hf-mirror"]
        with mock.patch("sys.argv", argv), mock.patch.object(
            huggingface_hub, "HfApi"
        ) as api, mock.patch.object(download, "_get_token", return_value=None):
            api.return_value.list_models.return_value = []
            self.assertEqual(download.main(), 0)
        api.assert_called_once_with(endpoint="https://hf-mirror.com", token=None)

    def test_mirror_reuses_hf_token(self):
        os.environ["HF_TOKEN"] = "test-token"
        with mock.patch.object(download, "_read_env_token", return_value=None):
            self.assertEqual(download._get_token("hf-mirror"), "test-token")
            self.assertEqual(download._get_token("hf-mirror", "explicit"), "explicit")

    def test_default_hf_source_still_selects_automatically(self):
        argv = ["download.py", "download", "org/model"]
        with mock.patch("sys.argv", argv), mock.patch.object(
            huggingface_hub, "snapshot_download"
        ) as snapshot, mock.patch.object(
            download, "_is_endpoint_reachable", side_effect=[False, True]
        ) as probe, mock.patch.object(download, "_get_token", return_value=None):
            self.assertEqual(download.main(), 0)
        self.assertEqual(snapshot.call_args.kwargs["endpoint"], download.HF_PRIMARY_ENDPOINT)
        self.assertEqual(probe.call_count, 2)


if __name__ == "__main__":
    unittest.main()
