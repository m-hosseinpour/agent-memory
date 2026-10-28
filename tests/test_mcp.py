"""Tests for the MCP stdio adapter: protocol, tools, and the real subprocess."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from memlib import config, mcp, store

REPO_ROOT = Path(__file__).resolve().parents[1]


class McpTestCase(unittest.TestCase):
    """Scratch store per test; every serve() response must be protocol JSON."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mem-mcp-")
        self.root = Path(self.tmp) / "memory"
        self.project = Path(self.tmp) / "project"
        self.project.mkdir()
        self._saved = {
            name: os.environ.get(name) for name in ("MEM_ROOT", "MEMORY_PROJECT_CWD")
        }
        os.environ["MEM_ROOT"] = str(self.root)
        os.environ["MEMORY_PROJECT_CWD"] = str(self.project)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def serve(self, messages=(), raw=None):
        payload = (
            raw if raw is not None else "".join(json.dumps(m) + "\n" for m in messages)
        )
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = mcp.serve(stdin=io.StringIO(payload), stdout=out)
        text = out.getvalue()
        responses = []
        for line in text.splitlines():
            self.assertTrue(line.strip(), "stdout contained a blank line")
            # json.loads fails if anything but JSON reached stdout.
            message = json.loads(line)
            self.assertIsInstance(message, dict)
            self.assertEqual(message.get("jsonrpc"), "2.0")
            responses.append(message)
        return code, responses, err.getvalue()

    def request(self, method, params=None, request_id=1):
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        return message

    def call(self, name, arguments, request_id=1):
        return self.request(
            "tools/call", {"name": name, "arguments": arguments}, request_id=request_id
        )

    def payload(self, response):
        self.assertIn("result", response, response)
        self.assertFalse(response["result"]["isError"], response)
        return json.loads(response["result"]["content"][0]["text"])

    def error_payload(self, response):
        self.assertIn("result", response, response)
        self.assertTrue(response["result"]["isError"], response)
        return json.loads(response["result"]["content"][0]["text"])

    def project_dir(self):
        return config.scope_dir("project", str(self.project))


class InitializeTests(McpTestCase):
    def test_handshake_echoes_the_requested_version_and_pings(self):
        code, responses, _ = self.serve(
            [
                self.request(
                    "initialize",
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "0"},
                    },
                    request_id="init-1",
                ),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                self.request("ping", request_id=2),
            ]
        )
        self.assertEqual(code, 0)
        # The initialized notification gets no response at all.
        self.assertEqual(len(responses), 2)
        init = responses[0]
        self.assertEqual(init["id"], "init-1")
        result = init["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "mem")
        self.assertTrue(result["serverInfo"]["version"])
        self.assertIn("memory_write", result["instructions"])
        # Every result names its type for the 2026-07-28 clients; ping has no
        # other fields.
        self.assertEqual(responses[1]["result"], {"resultType": "complete"})

    def test_unknown_protocol_version_falls_back_to_the_latest_legacy_one(self):
        _, responses, stderr = self.serve(
            [
                self.request(
                    "initialize", {"protocolVersion": "2026-07-28"}, request_id=1
                )
            ]
        )
        self.assertEqual(
            responses[0]["result"]["protocolVersion"], mcp.LATEST_PROTOCOL_VERSION
        )
        self.assertIn("unsupported protocol version", stderr)

    def test_older_legacy_version_is_echoed(self):
        _, responses, _ = self.serve(
            [
                self.request(
                    "initialize", {"protocolVersion": "2024-11-05"}, request_id=1
                )
            ]
        )
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2024-11-05")

    def test_initialize_requires_a_protocol_version(self):
        _, responses, _ = self.serve([self.request("initialize", {}, request_id=3)])
        self.assertEqual(responses[0]["error"]["code"], mcp.INVALID_PARAMS)
        self.assertIn("protocolVersion", responses[0]["error"]["message"])


class ToolsListTests(McpTestCase):
    def test_schema_matches_the_frozen_design(self):
        _, responses, _ = self.serve([self.request("tools/list")])
        tools = {tool["name"]: tool for tool in responses[0]["result"]["tools"]}
        self.assertEqual(
            sorted(tools), ["memory_index", "memory_read", "memory_write"]
        )
        for tool in tools.values():
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertFalse(schema["additionalProperties"])
            # A client that serves several projects cannot be told which one
            # otherwise; the argument is optional everywhere.
            self.assertIn("cwd", schema["properties"])
            self.assertNotIn("cwd", schema.get("required", []))

        index_tool = tools["memory_index"]["inputSchema"]
        self.assertEqual(set(index_tool["properties"]), {"cwd"})

        read = tools["memory_read"]["inputSchema"]
        self.assertEqual(read["required"], ["store", "path"])
        self.assertEqual(set(read["properties"]), {"store", "path", "cwd"})

        write = tools["memory_write"]["inputSchema"]
        self.assertEqual(
            set(write["required"]), {"store", "path", "content", "if_version"}
        )
        self.assertEqual(
            set(write["properties"]),
            {"store", "path", "content", "if_version", "name", "description", "type", "cwd"},
        )
        self.assertEqual(write["properties"]["store"]["enum"], ["global", "project"])
        self.assertEqual(write["properties"]["type"]["enum"], list(store.TYPES))

        # The instructions are the whole onboarding: no instruction file exists.
        init = self.serve(
            [self.request("initialize", {"protocolVersion": "2025-06-18"})]
        )[1][0]["result"]
        self.assertIn("memory_index", init["instructions"])
        self.assertIn("memory_write", init["instructions"])
        self.assertNotIn("PROTOCOL", init["instructions"])

    def test_the_param_table_matches_the_schemas(self):
        """_TOOL_PARAMS decides what an unknown argument is, so it must not
        drift from the published properties."""
        self.assertEqual(
            mcp._TOOL_PARAMS,
            {
                tool["name"]: set(tool["inputSchema"]["properties"])
                for tool in mcp.TOOLS
            },
        )


class UnknownArgumentTests(McpTestCase):
    """A name outside the schema is a request error, not something to ignore.

    The schemas say additionalProperties: false, so a conforming client never
    sends one. Ignoring it turned a misspelt `cwd` into a note in the server's
    own scope, with an ok result that looked like the intended write.
    """

    def write_arguments(self, **overrides):
        arguments = {
            "store": "project",
            "path": "typo.md",
            "content": "body",
            "if_version": "new",
            "name": "typo",
            "description": "a note",
        }
        arguments.update(overrides)
        return arguments

    def test_misspelt_cwd_is_invalid_params_and_writes_nothing(self):
        elsewhere = str(Path(self.tmp) / "elsewhere")
        _, responses, _ = self.serve(
            [
                self.call(
                    "memory_write",
                    self.write_arguments(cwdd=elsewhere),
                    request_id=1,
                ),
                self.request("ping", request_id=2),
            ]
        )
        error = responses[0]["error"]
        self.assertEqual(error["code"], mcp.INVALID_PARAMS)
        self.assertIn("cwdd", error["message"])
        self.assertIn("memory_write", error["message"])
        self.assertFalse(self.project_dir().exists())
        # The server keeps serving after a rejected call.
        self.assertEqual(responses[1]["result"], {"resultType": "complete"})

    def test_misspelt_path_is_invalid_params_on_read(self):
        _, responses, _ = self.serve(
            [
                self.call(
                    "memory_read",
                    {"store": "project", "pth": "typo.md"},
                    request_id=1,
                )
            ]
        )
        error = responses[0]["error"]
        self.assertEqual(error["code"], mcp.INVALID_PARAMS)
        self.assertIn("pth", error["message"])

    def test_each_tool_rejects_an_argument_it_does_not_take(self):
        for name, arguments, bad in (
            ("memory_index", {"cwdd": self.tmp}, "cwdd"),
            ("memory_read", {"store": "project", "path": "x.md", "extra": 1}, "extra"),
            ("memory_write", self.write_arguments(nope=True), "nope"),
        ):
            _, responses, _ = self.serve([self.call(name, arguments, request_id=1)])
            self.assertEqual(
                responses[0]["error"]["code"],
                mcp.INVALID_PARAMS,
                f"{name} accepted {bad}",
            )
            self.assertIn(bad, responses[0]["error"]["message"])
        self.assertFalse((self.project_dir() / "x.md").exists())

    def test_the_allowed_arguments_still_work_everywhere(self):
        self.serve(
            [
                self.call(
                    "memory_write",
                    self.write_arguments(cwd=str(self.project)),
                    request_id=1,
                )
            ]
        )
        _, responses, _ = self.serve(
            [
                self.call("memory_index", {"cwd": str(self.project)}, request_id=1),
                self.call(
                    "memory_read",
                    {"store": "project", "path": "typo.md", "cwd": str(self.project)},
                    request_id=2,
                ),
            ]
        )
        self.assertEqual(self.payload(responses[0])["count"], 1)
        self.assertEqual(self.payload(responses[1])["content"], "body")


class ReadWriteTests(McpTestCase):
    def write_new(self, path="pnpm-not-npm.md", **overrides):
        arguments = {
            "store": "project",
            "path": path,
            "content": "The lockfile is pnpm. npm install breaks CI.",
            "if_version": "new",
            "name": "pnpm-not-npm",
            "description": "the lockfile is pnpm; npm install breaks CI",
            "type": "project",
        }
        arguments.update(overrides)
        return arguments

    def test_write_then_read_round_trip(self):
        _, responses, _ = self.serve(
            [
                self.call("memory_write", self.write_new(), request_id=1),
                self.call(
                    "memory_read",
                    {"store": "project", "path": "pnpm-not-npm.md"},
                    request_id=2,
                ),
            ]
        )
        written = self.payload(responses[0])
        self.assertTrue(written["ok"])
        self.assertEqual(written["store"], "project")
        self.assertEqual(written["path"], "pnpm-not-npm.md")
        self.assertEqual(len(written["version"]), 12)

        document = self.project_dir() / "pnpm-not-npm.md"
        self.assertTrue(document.is_file())
        raw = document.read_text(encoding="utf-8")
        self.assertIn('name: "pnpm-not-npm"', raw)
        self.assertIn('type: "project"', raw)
        self.assertIn("The lockfile is pnpm.", raw)
        index_text = (self.project_dir() / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("(pnpm-not-npm.md)", index_text)
        self.assertIn("the lockfile is pnpm", index_text)

        read = self.payload(responses[1])
        self.assertEqual(read["path"], "pnpm-not-npm.md")
        self.assertEqual(read["file"], str(document))
        self.assertEqual(read["content"], "The lockfile is pnpm. npm install breaks CI.")
        self.assertEqual(read["version"], written["version"])
        self.assertEqual(read["type"], "project")
        self.assertEqual(read["description"], "the lockfile is pnpm; npm install breaks CI")
        self.assertEqual(read["name"], "pnpm-not-npm")
        self.assertTrue(read["modified"])

    def test_write_refreshes_the_index_line(self):
        _, responses, _ = self.serve(
            [self.call("memory_write", self.write_new(), request_id=1)]
        )
        first = self.payload(responses[0])
        arguments = self.write_new(
            content="Updated body.",
            if_version=first["version"],
            description="second description",
        )
        _, responses, _ = self.serve(
            [self.call("memory_write", arguments, request_id=2)]
        )
        self.payload(responses[0])
        index_text = (self.project_dir() / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("second description", index_text)
        self.assertNotIn("npm install breaks CI", index_text)
        self.assertEqual(index_text.count("- ["), 1)

    def test_rewrite_with_only_content_keeps_the_note(self):
        _, responses, _ = self.serve(
            [
                self.call(
                    "memory_write",
                    self.write_new(path="u.md", name="prefers-vim", type="user"),
                    request_id=1,
                )
            ]
        )
        first = self.payload(responses[0])
        _, responses, _ = self.serve(
            [
                self.call(
                    "memory_write",
                    {
                        "store": "project",
                        "path": "u.md",
                        "content": "x, fixed typo",
                        "if_version": first["version"],
                    },
                    request_id=2,
                )
            ]
        )
        self.payload(responses[0])
        memory = store.load(self.project_dir() / "u.md")
        self.assertEqual(
            (memory.name, memory.description, memory.type),
            (
                "prefers-vim",
                "the lockfile is pnpm; npm install breaks CI",
                "user",
            ),
        )
        index_text = (self.project_dir() / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("## user (1)", index_text)
        self.assertIn("the lockfile is pnpm; npm install breaks CI", index_text)

    def test_explicit_arguments_still_change_the_note(self):
        _, responses, _ = self.serve(
            [self.call("memory_write", self.write_new(), request_id=1)]
        )
        first = self.payload(responses[0])
        _, responses, _ = self.serve(
            [
                self.call(
                    "memory_write",
                    self.write_new(
                        content="Updated body.",
                        if_version=first["version"],
                        name="renamed",
                        description="second description",
                        type="reference",
                    ),
                    request_id=2,
                )
            ]
        )
        self.payload(responses[0])
        memory = store.load(self.project_dir() / "pnpm-not-npm.md")
        self.assertEqual(
            (memory.name, memory.description, memory.type),
            ("renamed", "second description", "reference"),
        )

    def test_bad_type_is_invalid_params_even_on_a_rewrite(self):
        _, responses, _ = self.serve(
            [self.call("memory_write", self.write_new(), request_id=1)]
        )
        first = self.payload(responses[0])
        _, responses, _ = self.serve(
            [
                self.call(
                    "memory_write",
                    self.write_new(
                        content="x", if_version=first["version"], type="gotcha"
                    ),
                    request_id=2,
                )
            ]
        )
        error = responses[0]["error"]
        self.assertEqual(error["code"], mcp.INVALID_PARAMS)
        self.assertEqual(store.load(self.project_dir() / "pnpm-not-npm.md").type, "project")

    def test_conflict_carries_current_content_and_version(self):
        _, responses, _ = self.serve(
            [self.call("memory_write", self.write_new(), request_id=1)]
        )
        first = self.payload(responses[0])
        second_arguments = self.write_new(
            content="Second body wins.", if_version=first["version"]
        )
        _, responses, _ = self.serve(
            [
                self.call("memory_write", second_arguments, request_id=2),
                # Stale write: carries the now-outdated first version.
                self.call(
                    "memory_write",
                    self.write_new(if_version=first["version"]),
                    request_id=3,
                ),
            ]
        )
        second = self.payload(responses[0])
        self.assertNotEqual(second["version"], first["version"])

        conflict = self.error_payload(responses[1])
        self.assertEqual(conflict["ok"], False)
        self.assertEqual(conflict["error"], "conflict")
        self.assertIn("re-read", conflict["message"])
        self.assertEqual(conflict["current"]["version"], second["version"])
        self.assertEqual(conflict["current"]["content"], "Second body wins.")
        self.assertIn("Second body wins.", conflict["current"]["raw"])

        # The refused write did not touch the file.
        self.assertEqual(
            store.load(self.project_dir() / "pnpm-not-npm.md").body, "Second body wins."
        )

    def test_conflict_payload_tolerates_non_utf8_bytes(self):
        _, responses, _ = self.serve(
            [self.call("memory_write", self.write_new(), request_id=1)]
        )
        first = self.payload(responses[0])
        document = self.project_dir() / "pnpm-not-npm.md"
        document.write_bytes(document.read_bytes() + b"\xff\n")
        _, responses, _ = self.serve(
            [self.call("memory_write", self.write_new(if_version=first["version"]), request_id=2)]
        )
        conflict = self.error_payload(responses[0])
        self.assertEqual(conflict["error"], "conflict")
        self.assertIn("\ufffd", conflict["current"]["raw"])

    def test_new_on_existing_file_conflicts(self):
        _, responses, _ = self.serve(
            [self.call("memory_write", self.write_new(), request_id=1)]
        )
        first = self.payload(responses[0])
        _, responses, _ = self.serve(
            [self.call("memory_write", self.write_new(), request_id=2)]
        )
        conflict = self.error_payload(responses[0])
        self.assertEqual(conflict["error"], "conflict")
        self.assertEqual(conflict["current"]["version"], first["version"])

    def test_secret_is_refused_and_not_written(self):
        secret = "api_key = 'abcdefghijklmnopqrstuvwxyz123456'"
        _, responses, _ = self.serve(
            [
                self.call(
                    "memory_write",
                    self.write_new(content=secret),
                    request_id=1,
                )
            ]
        )
        failure = self.error_payload(responses[0])
        self.assertEqual(failure["error"], "secret")
        self.assertIn("secret", failure["message"])
        self.assertFalse((self.project_dir() / "pnpm-not-npm.md").exists())

    def test_store_error_is_an_iserror_result(self):
        _, responses, _ = self.serve(
            [
                self.call(
                    "memory_write",
                    self.write_new(content="   \n"),
                    request_id=1,
                ),
                self.request("ping", request_id=2),
            ]
        )
        failure = self.error_payload(responses[0])
        self.assertEqual(failure["error"], "invalid")
        self.assertIn("empty", failure["message"])
        # A rejected document never takes the server down.
        # Every result names its type for the 2026-07-28 clients; ping has no
        # other fields.
        self.assertEqual(responses[1]["result"], {"resultType": "complete"})

    def test_read_of_a_missing_memory_is_a_tool_error(self):
        _, responses, _ = self.serve(
            [
                self.call(
                    "memory_read",
                    {"store": "project", "path": "nope.md"},
                    request_id=1,
                ),
                self.request("ping", request_id=2),
            ]
        )
        failure = self.error_payload(responses[0])
        self.assertEqual(failure["error"], "invalid")
        self.assertIn("no such memory", failure["message"])
        # The server keeps serving after a failed tool call.
        # Every result names its type for the 2026-07-28 clients; ping has no
        # other fields.
        self.assertEqual(responses[1]["result"], {"resultType": "complete"})

    def test_global_store_is_separate_from_project(self):
        _, responses, _ = self.serve(
            [
                self.call(
                    "memory_write",
                    self.write_new(
                        store="global",
                        path="prefers-vim.md",
                        name="prefers-vim",
                        description="prefers vim keybindings",
                    ),
                    request_id=1,
                ),
                self.call(
                    "memory_read",
                    {"store": "global", "path": "prefers-vim.md"},
                    request_id=2,
                ),
            ]
        )
        written = self.payload(responses[0])
        self.assertEqual(written["store"], "global")
        self.assertTrue((self.root / "global" / "prefers-vim.md").is_file())
        self.assertFalse((self.project_dir() / "prefers-vim.md").exists())
        self.assertEqual(self.payload(responses[1])["store"], "global")

    def test_unknown_store_is_rejected_with_a_readable_error(self):
        _, responses, _ = self.serve(
            [
                self.call(
                    "memory_read",
                    {"store": "workspace", "path": "x.md"},
                    request_id=1,
                )
            ]
        )
        error = responses[0]["error"]
        self.assertEqual(error["code"], mcp.INVALID_PARAMS)
        self.assertIn("global", error["message"])
        self.assertIn("project", error["message"])

    def test_path_traversal_and_generated_index_are_rejected(self):
        for path in ("../escape.md", "sub/dir.md", "MEMORY.md", "notes.txt"):
            _, responses, _ = self.serve(
                [
                    self.call(
                        "memory_read",
                        {"store": "project", "path": path},
                        request_id=1,
                    )
                ]
            )
            self.assertEqual(
                responses[0]["error"]["code"],
                mcp.INVALID_PARAMS,
                f"{path} was not rejected as invalid params",
            )

    def test_write_without_if_version_is_invalid_params(self):
        arguments = self.write_new()
        del arguments["if_version"]
        _, responses, _ = self.serve(
            [self.call("memory_write", arguments, request_id=1)]
        )
        error = responses[0]["error"]
        self.assertEqual(error["code"], mcp.INVALID_PARAMS)
        self.assertIn("if_version", error["message"])


class ProtocolTests(McpTestCase):
    def test_unknown_method_is_method_not_found(self):
        _, responses, _ = self.serve(
            [self.request("resources/list", request_id=9)]
        )
        self.assertEqual(responses[0]["error"]["code"], mcp.METHOD_NOT_FOUND)
        self.assertEqual(responses[0]["id"], 9)

    def test_notifications_get_no_response(self):
        _, responses, _ = self.serve(
            [
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}},
                {"jsonrpc": "2.0", "method": "tools/list"},  # unknown notification
                self.request("ping", request_id=1),
            ]
        )
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["id"], 1)

    def test_malformed_notification_is_ignored_silently(self):
        _, responses, stderr = self.serve(
            [
                {"jsonrpc": "1.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/whatever", "params": []},
                self.request("ping", request_id=1),
            ]
        )
        # A notification never gets a reply, not even for a malformed one.
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["id"], 1)
        self.assertIn("ignoring", stderr)

    def test_malformed_line_is_parse_error_and_the_loop_continues(self):
        _, responses, _ = self.serve(
            raw='{"jsonrpc": "2.0", "id": 1\n'
            + json.dumps(self.request("ping", request_id=2))
            + "\n"
        )
        self.assertEqual(responses[0]["error"]["code"], mcp.PARSE_ERROR)
        self.assertIsNone(responses[0]["id"])
        self.assertEqual(responses[1]["id"], 2)
        # Every result names its type for the 2026-07-28 clients; ping has no
        # other fields.
        self.assertEqual(responses[1]["result"], {"resultType": "complete"})

    def test_invalid_requests_are_invalid_request(self):
        _, responses, _ = self.serve(
            [
                {"id": 1, "method": "ping"},
                [1, 2, 3],
                {"jsonrpc": "2.0", "id": 2, "method": 42},
                {"jsonrpc": "1.0", "id": 3, "method": "ping"},
            ]
        )
        self.assertEqual(len(responses), 4)
        for response in responses:
            self.assertEqual(response["error"]["code"], mcp.INVALID_REQUEST)

    def test_unknown_tool_and_missing_arguments_are_invalid_params(self):
        _, responses, _ = self.serve(
            [
                self.call("memory_forget", {}, request_id=1),
                self.call("memory_write", {"store": "project", "path": "x.md"}, request_id=2),
                self.call(
                    "memory_read",
                    {"store": "project", "path": 7},
                    request_id=3,
                ),
            ]
        )
        self.assertEqual(len(responses), 3)
        for response in responses:
            self.assertEqual(response["error"]["code"], mcp.INVALID_PARAMS)
        self.assertIn("unknown tool", responses[0]["error"]["message"])
        self.assertIn("content", responses[1]["error"]["message"])
        self.assertIn("path", responses[2]["error"]["message"])

    def test_eof_returns_zero_and_an_unterminated_last_line_still_runs(self):
        code, responses, _ = self.serve(raw="")
        self.assertEqual(code, 0)
        self.assertEqual(responses, [])

        code, responses, _ = self.serve(
            raw=json.dumps(self.request("ping", request_id="last"))
        )
        self.assertEqual(code, 0)
        self.assertEqual(responses[0]["id"], "last")

    def test_stdout_carries_only_protocol_json(self):
        messages = [
            self.request("initialize", {"protocolVersion": "2025-06-18"}, request_id=1),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            self.request("tools/list", request_id=2),
            self.request("nope", request_id=3),
        ]
        code, responses, stderr = self.serve(raw="".join(json.dumps(m) + "\n" for m in messages) + "garbage\n")
        self.assertEqual(code, 0)
        self.assertEqual([response["id"] for response in responses], [1, 2, 3, None])
        self.assertNotIn("mem mcp:", "".join(json.dumps(r) for r in responses))
        self.assertIn("mem mcp:", stderr)


class IndexToolTests(McpTestCase):
    """memory_index is the whole retrieval path: it lists every note."""

    def write(self, path, name, description, store="project", body="body", **extra):
        arguments = {
            "store": store,
            "path": path,
            "content": body,
            "if_version": "new",
            "name": name,
            "description": description,
        }
        arguments.update(extra)
        return arguments

    def seed(self):
        self.serve(
            [
                self.call(
                    "memory_write",
                    self.write(
                        "pnpm-not-npm.md",
                        "pnpm-not-npm",
                        "the lockfile is pnpm; npm install breaks CI",
                        body="The lockfile is pnpm.",
                    ),
                    request_id=1,
                ),
                self.call(
                    "memory_write",
                    self.write(
                        "kde-fontconfig.md",
                        "kde-fontconfig",
                        "chromium poisons the fontconfig cache",
                        body="Chromium poisons the fontconfig cache.",
                    ),
                    request_id=2,
                ),
                self.call(
                    "memory_write",
                    self.write(
                        "prefers-vim.md",
                        "prefers-vim",
                        "vim keybindings everywhere",
                        store="global",
                        body="Prefers vim keybindings.",
                    ),
                    request_id=3,
                ),
            ]
        )

    def test_index_lists_both_stores(self):
        self.seed()
        _, responses, _ = self.serve([self.call("memory_index", {}, request_id=1)])
        result = self.payload(responses[0])
        self.assertEqual(result["count"], 3)
        self.assertIn("# global memory", result["index"])
        self.assertIn("# project memory", result["index"])
        self.assertIn("pnpm-not-npm.md", result["index"])
        self.assertIn("prefers-vim.md", result["index"])
        self.assertIn("the lockfile is pnpm", result["index"])

    def test_index_on_an_empty_store(self):
        _, responses, _ = self.serve([self.call("memory_index", {}, request_id=1)])
        result = self.payload(responses[0])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["index"], "")

    def test_index_honours_an_explicit_cwd(self):
        self.seed()
        other = Path(self.tmp) / "other"
        other.mkdir()
        _, responses, _ = self.serve(
            [self.call("memory_index", {"cwd": str(other)}, request_id=1)]
        )
        result = self.payload(responses[0])
        # The project scope moved; the global scope did not.
        self.assertEqual(result["count"], 1)
        self.assertNotIn("pnpm-not-npm.md", result["index"])
        self.assertIn("prefers-vim.md", result["index"])

    def test_write_honours_an_explicit_cwd(self):
        other = Path(self.tmp) / "other"
        arguments = self.write(
            "elsewhere.md", "elsewhere", "a note for another project", cwd=str(other)
        )
        _, responses, _ = self.serve(
            [self.call("memory_write", arguments, request_id=1)]
        )
        written = self.payload(responses[0])
        self.assertEqual(
            Path(written["file"]).parent, config.scope_dir("project", str(other))
        )
        # MEMORY_PROJECT_CWD is only the fallback: the explicit cwd wins.
        self.assertFalse((self.project_dir() / "elsewhere.md").exists())

    def test_write_consolidates_the_scope(self):
        self.serve(
            [
                self.call(
                    "memory_write",
                    self.write("a.md", "shared", "same note"),
                    request_id=1,
                )
            ]
        )
        _, responses, _ = self.serve(
            [
                self.call(
                    "memory_write",
                    self.write("b.md", "shared", "same note"),
                    request_id=1,
                )
            ]
        )
        self.payload(responses[0])
        self.assertFalse((self.project_dir() / "a.md").exists())
        self.assertTrue((self.project_dir() / "b.md").exists())


class DiscoveryTests(McpTestCase):
    """The modern (2026-07-28) probe, served next to the legacy handshake."""

    def test_discover_advertises_both_eras(self):
        code, responses, _ = self.serve(
            [{"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}}]
        )
        self.assertEqual(code, 0)
        self.assertEqual(responses[0]["id"], 1)
        result = responses[0]["result"]
        self.assertIn("2026-07-28", result["supportedVersions"])
        self.assertIn("2025-11-25", result["supportedVersions"])
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["ttlMs"], 0)
        self.assertTrue(result["instructions"])

    def test_a_modern_client_can_skip_the_handshake(self):
        code, responses, _ = self.serve(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "memory_index", "arguments": {}},
                },
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual([r["id"] for r in responses], [1, 2, 3])
        self.assertEqual(
            {tool["name"] for tool in responses[1]["result"]["tools"]},
            {"memory_index", "memory_read", "memory_write"},
        )
        self.assertFalse(responses[2]["result"]["isError"])

    def test_list_results_carry_the_modern_cache_and_type_fields(self):
        """A 2026-07-28 client rejects a result without these (verified against
        Claude Code 2.1.301, which named the missing field)."""
        code, responses, _ = self.serve(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
        )
        self.assertEqual(code, 0)
        discover, listing = responses[0]["result"], responses[1]["result"]
        for result in (discover, listing):
            self.assertEqual(result["resultType"], "complete")
            self.assertIsInstance(result["ttlMs"], int)
            self.assertIn(result["cacheScope"], ("public", "private"))
        self.assertEqual(discover["ttlMs"], 0)
        self.assertEqual(listing["ttlMs"], 0)

    def test_initialize_still_answers_a_modern_version(self):
        code, responses, err = self.serve(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2026-07-28",
                        "capabilities": {},
                        "clientInfo": {"name": "legacy", "version": "1"},
                    },
                }
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertIn("unsupported protocol version", err)


class SubprocessTests(McpTestCase):
    def test_bin_mem_mcp_speaks_line_delimited_json(self):
        launcher = REPO_ROOT / "bin" / "mem"
        messages = [
            self.request("initialize", {"protocolVersion": "2025-06-18"}, request_id=1),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            self.request("tools/list", request_id=2),
            self.call(
                "memory_write",
                {
                    "store": "project",
                    "path": "from-subprocess.md",
                    "content": "Written through the real process.",
                    "if_version": "new",
                    "name": "from-subprocess",
                    "description": "a memory written over stdio",
                },
                request_id=3,
            ),
            self.call(
                "memory_read",
                {"store": "project", "path": "from-subprocess.md"},
                request_id=4,
            ),
        ]
        env = os.environ.copy()
        env["MEM_ROOT"] = str(self.root)
        env["MEMORY_PROJECT_CWD"] = str(self.project)
        process = subprocess.run(
            ["bash", str(launcher), "mcp"],
            input="".join(json.dumps(message) + "\n" for message in messages),
            text=True,
            capture_output=True,
            cwd=str(self.project),
            env=env,
            timeout=60,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("mem mcp:", process.stderr)

        lines = process.stdout.splitlines()
        self.assertEqual(len(lines), 4)
        responses = [json.loads(line) for line in lines]
        self.assertEqual([response["id"] for response in responses], [1, 2, 3, 4])
        self.assertEqual(
            responses[0]["result"]["protocolVersion"], "2025-06-18"
        )
        written = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertFalse(responses[2]["result"]["isError"])
        self.assertEqual(len(written["version"]), 12)
        read = json.loads(responses[3]["result"]["content"][0]["text"])
        self.assertEqual(read["content"], "Written through the real process.")
        self.assertEqual(read["version"], written["version"])

        self.assertTrue((self.project_dir() / "from-subprocess.md").is_file())
        self.assertTrue((self.project_dir() / "MEMORY.md").is_file())


if __name__ == "__main__":
    unittest.main()
