"""Audio playback for alerts."""

import logging
import threading
from pathlib import Path
from typing import Optional

from scanner_service.settings import get_settings

logger = logging.getLogger(__name__)


class AudioPlayer:
    """
    Non-blocking audio player for alert sounds.

    Uses platform-appropriate methods for audio playback.
    Rate-limited to prevent audio spam.
    """

    def __init__(self):
        self.settings = get_settings()
        self._enabled = True
        self._last_play: dict[str, float] = {}
        self._min_interval = 0.5  # Minimum seconds between same sound
        self._lock = threading.Lock()

    @property
    def sounds_dir(self) -> Path:
        """Get sounds directory path."""
        return self.settings.sounds_dir

    def play(self, filename: str) -> bool:
        """
        Play a sound file (non-blocking).

        Args:
            filename: Name of sound file in sounds directory

        Returns:
            True if playback initiated, False otherwise
        """
        if not self._enabled:
            return False

        sound_path = self.sounds_dir / filename
        if not sound_path.exists():
            logger.warning(f"Sound file not found: {sound_path}")
            return False

        # Rate limiting
        import time
        now = time.time()
        with self._lock:
            last = self._last_play.get(filename, 0)
            if now - last < self._min_interval:
                return False
            self._last_play[filename] = now

        # Play in background thread
        thread = threading.Thread(
            target=self._play_sound,
            args=(sound_path,),
            daemon=True,
        )
        thread.start()

        return True

    def _play_sound(self, path: Path) -> None:
        """Play sound file (runs in thread)."""
        try:
            # Try different playback methods
            if self._try_winsound(path):
                return
            if self._try_playsound(path):
                return
            if self._try_pygame(path):
                return
            if self._try_system(path):
                return

            logger.warning(f"No audio backend available for: {path}")

        except Exception as e:
            logger.error(f"Audio playback error: {e}")

    def _try_winsound(self, path: Path) -> bool:
        """Try Windows winsound module."""
        try:
            import winsound
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            return True
        except (ImportError, RuntimeError):
            return False

    def _try_playsound(self, path: Path) -> bool:
        """Try playsound library."""
        try:
            from playsound import playsound
            playsound(str(path), block=False)
            return True
        except ImportError:
            return False
        except Exception as e:
            logger.debug(f"playsound failed: {e}")
            return False

    def _try_pygame(self, path: Path) -> bool:
        """Try pygame mixer."""
        try:
            import pygame
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            sound = pygame.mixer.Sound(str(path))
            sound.play()
            return True
        except ImportError:
            return False
        except Exception as e:
            logger.debug(f"pygame failed: {e}")
            return False

    def _try_system(self, path: Path) -> bool:
        """Try system command as last resort."""
        import subprocess
        import platform

        system = platform.system()
        try:
            if system == "Windows":
                # PowerShell method
                subprocess.Popen(
                    ["powershell", "-c", f"(New-Object Media.SoundPlayer '{path}').PlaySync()"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            elif system == "Darwin":  # macOS
                subprocess.Popen(
                    ["afplay", str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            elif system == "Linux":
                subprocess.Popen(
                    ["aplay", str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
        except Exception as e:
            logger.debug(f"System audio failed: {e}")

        return False

    def enable(self) -> None:
        """Enable audio playback."""
        self._enabled = True

    def disable(self) -> None:
        """Disable audio playback."""
        self._enabled = False

    def is_enabled(self) -> bool:
        """Check if audio is enabled."""
        return self._enabled

    def list_sounds(self) -> list[str]:
        """List available sound files."""
        if not self.sounds_dir.exists():
            return []
        return [f.name for f in self.sounds_dir.glob("*.wav")]

    def test_all(self) -> dict[str, bool]:
        """Test all available sounds."""
        results = {}
        for sound in self.list_sounds():
            results[sound] = self.play(sound)
            import time
            time.sleep(1)  # Brief pause between sounds
        return results
