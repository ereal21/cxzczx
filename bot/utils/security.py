"""Security utilities for user verification and IP monitoring."""

from __future__ import annotations

import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from io import BytesIO
from typing import Deque, Dict, Optional

try:  # Pillow is required for rendering the CAPTCHA challenges
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover - Pillow should be available but keep guard
    Image = ImageDraw = ImageFont = None


from typing import Deque, Dict, Optional

from bot.logger_mesh import logger


@dataclass
class VerificationChallenge:
    """Represents an onboarding challenge for a Telegram user."""

    question: str
    answer: str
    referral: Optional[str] = None
    captcha_verified: bool = False
    photo_verified: bool = False
    attempts: int = 0
    last_failures: Deque[float] = field(default_factory=lambda: deque(maxlen=10))
    created_at: float = field(default_factory=time.time)


class SecurityManager:
    """Centralised security helpers for the bot and web services."""

    _user_challenges: Dict[int, VerificationChallenge] = {}
    _verified_users: set[int] = set()
    _blocked_users: Dict[int, float] = {}

    _ip_requests: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=200))
    _ip_failures: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=50))
    _blocked_ips: Dict[str, float] = {}

    captcha_attempt_limit: int = 3
    photo_attempt_limit: int = 2
    block_duration: float = 15 * 60  # 15 minutes

    ip_rate_window: float = 60.0
    ip_rate_limit: int = 30
    ip_failure_limit: int = 6
    ip_failure_window: float = 10 * 60.0
    ip_anomaly_threshold: int = 120

    @staticmethod
    def _generate_captcha() -> tuple[str, str]:
        left = random.randint(10, 99)
        right = random.randint(1, 9)
        if random.random() > 0.5:
            question = f"{left} + {right}"
            answer = str(left + right)
        else:
            question = f"{left} - {right}"
            answer = str(left - right)
        return question, answer

    @classmethod
    def ensure_challenge(cls, user_id: int, referral: Optional[str] = None) -> VerificationChallenge:
        challenge = cls._user_challenges.get(user_id)
        if not challenge:
            question, answer = cls._generate_captcha()
            challenge = VerificationChallenge(question=question, answer=answer, referral=referral)
            cls._user_challenges[user_id] = challenge
            logger.info("Created security challenge for user %s", user_id)
        elif referral and not challenge.referral:
            challenge.referral = referral
        return challenge

    @classmethod
    def refresh_captcha(cls, user_id: int) -> VerificationChallenge:
        question, answer = cls._generate_captcha()
        challenge = cls._user_challenges.setdefault(
            user_id, VerificationChallenge(question=question, answer=answer)
        )
        challenge.question = question
        challenge.answer = answer
        challenge.captcha_verified = False
        logger.debug("Refreshed captcha for user %s", user_id)
        return challenge

    @classmethod
    def build_captcha_image(
        cls, user_id: int, challenge: Optional[VerificationChallenge] = None
    ) -> BytesIO:
        """Return an image containing the CAPTCHA question."""

        challenge = challenge or cls.ensure_challenge(user_id)

        if Image is None:
            raise RuntimeError("Pillow must be installed to generate verification CAPTCHA images.")

        # Default dimensions chosen to work with Telegram's image preview sizes.
        width, height = 420, 180
        background_color = (247, 247, 247)
        text_color = (20, 20, 20)

        image = Image.new("RGB", (width, height), background_color)
        draw = ImageDraw.Draw(image)

        try:
            font = ImageFont.truetype("arial.ttf", 64)
        except Exception:
            font = ImageFont.load_default()

        text = challenge.question
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = (width - text_width) / 2
        y = (height - text_height) / 2
        draw.text((x, y), text, font=font, fill=text_color)

        # Add minimal noise so automated bots struggle while humans can read easily.
        for _ in range(5):
            x1 = random.randint(0, width)
            y1 = random.randint(0, height)
            x2 = random.randint(0, width)
            y2 = random.randint(0, height)
            draw.line((x1, y1, x2, y2), fill=(160, 160, 160), width=2)

        for _ in range(80):
            dot_x = random.randint(0, width - 1)
            dot_y = random.randint(0, height - 1)
            draw.point((dot_x, dot_y), fill=(200, 200, 200))

        buffer = BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        buffer.name = "captcha.png"
        return buffer


    @classmethod

    def submit_captcha(cls, user_id: int, answer: str) -> bool:
        challenge = cls._user_challenges.get(user_id)
        if not challenge:
            challenge = cls.refresh_captcha(user_id)
        if cls.is_user_blocked(user_id):
            return False
        normalized = answer.strip().lower()
        if normalized == challenge.answer.lower():
            challenge.captcha_verified = True
            challenge.attempts = 0
            challenge.last_failures.clear()
            logger.info("User %s solved captcha", user_id)
            return True
        challenge.attempts += 1
        now = time.time()
        challenge.last_failures.append(now)
        logger.warning(
            "User %s failed captcha attempt %s", user_id, challenge.attempts
        )
        if challenge.attempts >= cls.captcha_attempt_limit:
            cls.block_user(user_id, reason="too_many_captcha_failures")
        else:
            cls.refresh_captcha(user_id)
        return False

    @classmethod
    def mark_photo_received(cls, user_id: int) -> bool:
        challenge = cls._user_challenges.get(user_id)
        if not challenge:
            challenge = cls.refresh_captcha(user_id)
        if cls.is_user_blocked(user_id):
            return False
        challenge.photo_verified = True
        challenge.attempts = 0
        cls._verified_users.add(user_id)
        logger.info("User %s passed photo verification", user_id)
        return True

    @classmethod
    def register_failed_photo(cls, user_id: int) -> None:
        challenge = cls._user_challenges.get(user_id)
        if not challenge:
            challenge = cls.refresh_captcha(user_id)
        challenge.attempts += 1
        logger.warning(
            "User %s submitted invalid verification photo (attempt %s)",
            user_id,
            challenge.attempts,
        )
        if challenge.attempts >= cls.photo_attempt_limit:
            cls.block_user(user_id, reason="invalid_photo_attempts")

    @classmethod
    def clear_challenge(cls, user_id: int) -> None:
        cls._user_challenges.pop(user_id, None)

    @classmethod
    def is_verified(cls, user_id: int) -> bool:
        if cls.is_user_blocked(user_id):
            return False
        challenge = cls._user_challenges.get(user_id)
        if challenge and challenge.captcha_verified:
            challenge.photo_verified = True
            cls._verified_users.add(user_id)
        return user_id in cls._verified_users

    @classmethod
    def get_referral(cls, user_id: int) -> Optional[str]:
        challenge = cls._user_challenges.get(user_id)
        return challenge.referral if challenge else None

    @classmethod
    def pop_referral(cls, user_id: int) -> Optional[str]:
        challenge = cls._user_challenges.get(user_id)
        if not challenge:
            return None
        referral = challenge.referral
        challenge.referral = None
        return referral

    @classmethod
    def block_user(cls, user_id: int, reason: str, duration: Optional[float] = None) -> None:
        expiry = time.time() + (duration or cls.block_duration)
        cls._blocked_users[user_id] = expiry
        logger.error(
            "Blocking user %s for %ss due to %s",
            user_id,
            int(expiry - time.time()),
            reason,
        )

    @classmethod
    def is_user_blocked(cls, user_id: int) -> bool:
        expiry = cls._blocked_users.get(user_id)
        if not expiry:
            return False
        if expiry <= time.time():
            cls._blocked_users.pop(user_id, None)
            return False
        return True

    @classmethod
    def user_block_message(cls, user_id: int) -> str:
        expiry = cls._blocked_users.get(user_id)
        if not expiry:
            return "🚫 Access denied."
        remaining = max(int(expiry - time.time()), 0)
        minutes, seconds = divmod(remaining, 60)
        duration = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
        return (
            "🚫 Due to suspicious activity, access is temporarily blocked. "
            f"Please try again in {duration}."
        )

    @classmethod
    def record_ip_request(cls, ip: str) -> tuple[bool, Optional[str]]:
        now = time.time()
        queue = cls._ip_requests[ip]
        queue.append(now)
        while queue and now - queue[0] > cls.ip_rate_window:
            queue.popleft()
        if len(queue) > cls.ip_anomaly_threshold:
            cls.block_ip(ip, reason="anomalous_request_rate", duration=cls.block_duration * 2)
            return False, "anomaly"
        if len(queue) > cls.ip_rate_limit:
            return False, "rate_limit"
        return True, None

    @classmethod
    def block_ip(cls, ip: str, reason: str, duration: Optional[float] = None) -> None:
        expiry = time.time() + (duration or cls.block_duration)
        cls._blocked_ips[ip] = expiry
        logger.error("Blocked IP %s for %ss due to %s", ip, int(expiry - time.time()), reason)

    @classmethod
    def is_ip_blocked(cls, ip: str) -> bool:
        expiry = cls._blocked_ips.get(ip)
        if not expiry:
            return False
        if expiry <= time.time():
            cls._blocked_ips.pop(ip, None)
            return False
        return True

    @classmethod
    def record_ip_failure(cls, ip: str, reason: str) -> None:
        now = time.time()
        failure_log = cls._ip_failures[ip]
        failure_log.append(now)
        while failure_log and now - failure_log[0] > cls.ip_failure_window:
            failure_log.popleft()
        logger.warning(
            "Security failure for IP %s: %s (%s recent failures)",
            ip,
            reason,
            len(failure_log),
        )
        if len(failure_log) >= cls.ip_failure_limit:
            cls.block_ip(ip, reason=f"repeated_failures:{reason}")

    @classmethod
    def cleanup(cls) -> None:
        now = time.time()
        for user_id, expiry in list(cls._blocked_users.items()):
            if expiry <= now:
                cls._blocked_users.pop(user_id, None)
        for ip, expiry in list(cls._blocked_ips.items()):
            if expiry <= now:
                cls._blocked_ips.pop(ip, None)


__all__ = ["SecurityManager", "VerificationChallenge"]
