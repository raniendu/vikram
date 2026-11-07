import json
import tempfile
import unittest
from pathlib import Path

from vikram.resources import ProjectResources
from vikram.settings import Settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.settings = Settings(self.root / "user", self.workspace)

    def test_project_needs_persisted_canonical_trust_and_cannot_grant_authority(self):
        directory = self.workspace / ".vikram"
        directory.mkdir()
        (directory / "settings.json").write_text(
            json.dumps({"theme": "light", "trusted": True, "model": "openrouter:x"})
        )
        self.settings.set("theme", "dark")
        self.assertEqual(self.settings.get("theme"), "dark")
        self.assertFalse(self.settings.is_trusted())
        self.settings.trust()
        reloaded = Settings(self.root / "user", self.workspace)
        self.assertTrue(reloaded.is_trusted())
        self.assertEqual(reloaded.get("theme"), "light")
        self.assertNotEqual(reloaded.get("model"), "openrouter:x")
        self.assertNotIn("trusted", reloaded.load())
        self.assertEqual(reloaded.load({"theme": "custom"})["theme"], "custom")
        reloaded.untrust()
        self.assertFalse(self.settings.is_trusted())

    def test_symlink_spelling_does_not_create_a_separate_trust_identity(self):
        alias = self.root / "alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        self.settings.trust(alias)
        self.assertTrue(self.settings.is_trusted(self.workspace))
        with self.assertRaises(ValueError):
            self.settings.set("api_key", "never-store")
        with self.assertRaises(ValueError):
            self.settings.set(
                "mcp_servers", {"server": {"headers": {"Authorization": "never-store"}}}
            )

    def test_malformed_settings_and_bare_ollama_model_tags(self):
        self.settings.trust()
        self.settings.set("model", "gemma4:26b", scope="project")
        self.assertEqual(self.settings.get("model"), "gemma4:26b")
        self.settings.root.mkdir(exist_ok=True)
        (self.settings.root / "settings.json").write_text(
            '{"interface": [], "theme": "dark"}'
        )
        self.assertEqual(self.settings.get("interface"), "fullscreen")
        self.assertEqual(self.settings.get("theme"), "dark")
        with self.assertRaises(ValueError):
            self.settings.set("model", "openrouter:vendor/model", scope="project")
        for model in (" openrouter:vendor/model", "\topenrouter:vendor/model\n"):
            with self.assertRaises(ValueError):
                self.settings.set("model", model, scope="project")
            with self.assertRaises(ValueError):
                self.settings.set("models", [model], scope="project")

    def test_resources_are_trusted_bounded_lazy_and_hierarchical(self):
        (self.workspace / "AGENTS.md").write_text("Project instructions")
        nested = self.workspace / "src"
        nested.mkdir()
        (nested / "AGENTS.md").write_text("Nested instructions")
        skill = self.workspace / ".vikram" / "skills" / "test"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: test\ndescription: Test the project\n---\nLazy body"
        )
        resources = ProjectResources(self.settings)
        self.assertEqual(resources.instructions(nested), "")
        self.assertEqual(resources.reload().skills, ())
        self.settings.trust()
        self.assertEqual(
            resources.instructions(nested),
            "Project instructions\n\nNested instructions",
        )
        catalog = resources.reload()
        self.assertEqual(catalog.skills[0].name, "test")
        self.assertFalse(hasattr(catalog.skills[0], "body"))
        self.assertIn("Lazy body", resources.read_skill("test"))
        (skill / "SKILL.md").unlink()
        (skill / "SKILL.md").symlink_to(self.root / "private")
        (self.root / "private").write_text("not-a-project-resource")
        self.assertEqual(resources.reload().skills, ())


if __name__ == "__main__":
    unittest.main()
