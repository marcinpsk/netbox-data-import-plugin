# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Authored refusal messages that HTTP responses and Jobs may show to the operator."""


class PublicRefusal(Exception):
    """Carry public text separately from the exception's internal diagnostic representation."""

    def __init__(self, message: str):
        super().__init__(message)
        self.operator_message = message
