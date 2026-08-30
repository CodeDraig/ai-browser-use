"""Low-level keyboard mechanics owned by the actor layer."""

import asyncio

from cdp_use.cdp.input.commands import DispatchKeyEventParameters

from browser_use.actor.utils import Utils


class KeyboardInteractor:
	"""Dispatch page text, key chords, special keys, and character sequences."""

	def __init__(self, browser_session) -> None:
		self.browser_session = browser_session

	@property
	def logger(self):
		return self.browser_session.logger

	async def type_to_page(self, text: str):
		"""
		Type text to the page (whatever element currently has focus).
		This is used when index is 0 or when an element can't be found.
		"""
		try:
			# Get CDP client and session
			cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=None, focus=True)

			# Type the text character by character to the focused element
			for char in text:
				# Handle newline characters as Enter key
				if char == '\n':
					# Send proper Enter key sequence
					await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'keyDown',
							'key': 'Enter',
							'code': 'Enter',
							'windowsVirtualKeyCode': 13,
						},
						session_id=cdp_session.session_id,
					)
					# Send char event with carriage return
					await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'char',
							'text': '\r',
						},
						session_id=cdp_session.session_id,
					)
					# Send keyup
					await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'keyUp',
							'key': 'Enter',
							'code': 'Enter',
							'windowsVirtualKeyCode': 13,
						},
						session_id=cdp_session.session_id,
					)
				else:
					# Handle regular characters
					# Send keydown
					await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'keyDown',
							'key': char,
						},
						session_id=cdp_session.session_id,
					)
					# Send char for actual text input
					await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'char',
							'text': char,
						},
						session_id=cdp_session.session_id,
					)
					# Send keyup
					await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'keyUp',
							'key': char,
						},
						session_id=cdp_session.session_id,
					)
				# Add 10ms delay between keystrokes
				await asyncio.sleep(0.010)
		except Exception as e:
			raise Exception(f'Failed to type to page: {str(e)}')

	def character_key_info(self, char: str) -> tuple[int, int, str]:
		"""Get modifiers, virtual key code, and base key for a character.

		Returns:
			(modifiers, windowsVirtualKeyCode, base_key)
		"""
		# Characters that require Shift modifier
		shift_chars = {
			'!': ('1', 49),
			'@': ('2', 50),
			'#': ('3', 51),
			'$': ('4', 52),
			'%': ('5', 53),
			'^': ('6', 54),
			'&': ('7', 55),
			'*': ('8', 56),
			'(': ('9', 57),
			')': ('0', 48),
			'_': ('-', 189),
			'+': ('=', 187),
			'{': ('[', 219),
			'}': (']', 221),
			'|': ('\\', 220),
			':': (';', 186),
			'"': ("'", 222),
			'<': (',', 188),
			'>': ('.', 190),
			'?': ('/', 191),
			'~': ('`', 192),
		}

		# Check if character requires Shift
		if char in shift_chars:
			base_key, vk_code = shift_chars[char]
			return (8, vk_code, base_key)  # Shift=8

		# Some Unicode characters' upper()/lower() expand to multiple code points
		# (e.g. 'ß'.upper() == 'SS', 'ﬃ'.upper() == 'FFI'). ord() rejects those,
		# so fall back to the original char's code point for the VK code.
		def _vk_from(c: str) -> int:
			up = c.upper()
			return ord(up) if len(up) == 1 else ord(c)

		# Uppercase letters require Shift
		if char.isupper():
			return (8, ord(char), char.lower()[:1] or char)  # Shift=8

		# Lowercase letters
		if char.islower():
			return (0, _vk_from(char), char)

		# Numbers
		if char.isdigit():
			return (0, ord(char), char)

		# Special characters without Shift
		no_shift_chars = {
			' ': 32,
			'-': 189,
			'=': 187,
			'[': 219,
			']': 221,
			'\\': 220,
			';': 186,
			"'": 222,
			',': 188,
			'.': 190,
			'/': 191,
			'`': 192,
		}

		if char in no_shift_chars:
			return (0, no_shift_chars[char], char)

		# Fallback
		return (0, _vk_from(char) if char.isalpha() else ord(char), char)

	def key_code_for_character(self, char: str) -> str:
		"""Get the proper key code for a character (like Playwright does)."""
		# Key code mapping for common characters (using proper base keys + modifiers)
		key_codes = {
			' ': 'Space',
			'.': 'Period',
			',': 'Comma',
			'-': 'Minus',
			'_': 'Minus',  # Underscore uses Minus with Shift
			'@': 'Digit2',  # @ uses Digit2 with Shift
			'!': 'Digit1',  # ! uses Digit1 with Shift (not 'Exclamation')
			'?': 'Slash',  # ? uses Slash with Shift
			':': 'Semicolon',  # : uses Semicolon with Shift
			';': 'Semicolon',
			'(': 'Digit9',  # ( uses Digit9 with Shift
			')': 'Digit0',  # ) uses Digit0 with Shift
			'[': 'BracketLeft',
			']': 'BracketRight',
			'{': 'BracketLeft',  # { uses BracketLeft with Shift
			'}': 'BracketRight',  # } uses BracketRight with Shift
			'/': 'Slash',
			'\\': 'Backslash',
			'=': 'Equal',
			'+': 'Equal',  # + uses Equal with Shift
			'*': 'Digit8',  # * uses Digit8 with Shift
			'&': 'Digit7',  # & uses Digit7 with Shift
			'%': 'Digit5',  # % uses Digit5 with Shift
			'$': 'Digit4',  # $ uses Digit4 with Shift
			'#': 'Digit3',  # # uses Digit3 with Shift
			'^': 'Digit6',  # ^ uses Digit6 with Shift
			'~': 'Backquote',  # ~ uses Backquote with Shift
			'`': 'Backquote',
			"'": 'Quote',
			'"': 'Quote',  # " uses Quote with Shift
		}

		# Numbers
		if char.isdigit():
			return f'Digit{char}'

		# Letters
		if char.isalpha():
			return f'Key{char.upper()}'

		# Special characters
		if char in key_codes:
			return key_codes[char]

		# Fallback for unknown characters
		return f'Key{char.upper()}'

	async def _dispatch_key_event(self, cdp_session, event_type: str, key: str, modifiers: int = 0) -> None:
		"""Helper to dispatch a keyboard event with proper key codes."""
		code, vk_code = Utils.get_key_info(key)
		params: DispatchKeyEventParameters = {
			'type': event_type,
			'key': key,
			'code': code,
		}
		if modifiers:
			params['modifiers'] = modifiers
		if vk_code is not None:
			params['windowsVirtualKeyCode'] = vk_code
		await cdp_session.cdp_client.send.Input.dispatchKeyEvent(params=params, session_id=cdp_session.session_id)

	async def send_keys(self, keys: str) -> None:
		"""Handle send keys request with CDP."""
		cdp_session = await self.browser_session.get_or_create_cdp_session(focus=True)
		try:
			# Normalize key names from common aliases
			key_aliases = {
				'ctrl': 'Control',
				'control': 'Control',
				'alt': 'Alt',
				'option': 'Alt',
				'meta': 'Meta',
				'cmd': 'Meta',
				'command': 'Meta',
				'shift': 'Shift',
				'enter': 'Enter',
				'return': 'Enter',
				'tab': 'Tab',
				'delete': 'Delete',
				'backspace': 'Backspace',
				'escape': 'Escape',
				'esc': 'Escape',
				'space': ' ',
				'up': 'ArrowUp',
				'down': 'ArrowDown',
				'left': 'ArrowLeft',
				'right': 'ArrowRight',
				'pageup': 'PageUp',
				'pagedown': 'PageDown',
				'home': 'Home',
				'end': 'End',
			}

			# Parse and normalize the key string
			keys = keys
			if '+' in keys:
				# Handle key combinations like "ctrl+a"
				parts = keys.split('+')
				normalized_parts = []
				for part in parts:
					part_lower = part.strip().lower()
					normalized = key_aliases.get(part_lower, part)
					normalized_parts.append(normalized)
				normalized_keys = '+'.join(normalized_parts)
			else:
				# Single key
				keys_lower = keys.strip().lower()
				normalized_keys = key_aliases.get(keys_lower, keys)

			# Handle key combinations like "Control+A"
			if '+' in normalized_keys:
				parts = normalized_keys.split('+')
				modifiers = parts[:-1]
				main_key = parts[-1]

				# Calculate modifier bitmask
				modifier_value = 0
				modifier_map = {'Alt': 1, 'Control': 2, 'Meta': 4, 'Shift': 8}
				for mod in modifiers:
					modifier_value |= modifier_map.get(mod, 0)

				# Press modifier keys
				for mod in modifiers:
					await self._dispatch_key_event(cdp_session, 'keyDown', mod)

				# Press main key with modifiers bitmask
				await self._dispatch_key_event(cdp_session, 'keyDown', main_key, modifier_value)

				await self._dispatch_key_event(cdp_session, 'keyUp', main_key, modifier_value)

				# Release modifier keys
				for mod in reversed(modifiers):
					await self._dispatch_key_event(cdp_session, 'keyUp', mod)
			else:
				# Check if this is a text string or special key
				special_keys = {
					'Enter',
					'Tab',
					'Delete',
					'Backspace',
					'Escape',
					'ArrowUp',
					'ArrowDown',
					'ArrowLeft',
					'ArrowRight',
					'PageUp',
					'PageDown',
					'Home',
					'End',
					'Control',
					'Alt',
					'Meta',
					'Shift',
					'F1',
					'F2',
					'F3',
					'F4',
					'F5',
					'F6',
					'F7',
					'F8',
					'F9',
					'F10',
					'F11',
					'F12',
				}

				# If it's a special key, use original logic
				if normalized_keys in special_keys:
					await self._dispatch_key_event(cdp_session, 'keyDown', normalized_keys)
					# For Enter key, also dispatch a char event to trigger keypress listeners
					if normalized_keys == 'Enter':
						await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
							params={
								'type': 'char',
								'text': '\r',
								'key': 'Enter',
							},
							session_id=cdp_session.session_id,
						)
					await self._dispatch_key_event(cdp_session, 'keyUp', normalized_keys)
				else:
					# It's text (single character or string) - send each character as text input
					# This is crucial for text to appear in focused input fields
					for char in normalized_keys:
						# Special-case newline characters to dispatch as Enter
						if char in ('\n', '\r'):
							await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
								params={
									'type': 'rawKeyDown',
									'windowsVirtualKeyCode': 13,
									'unmodifiedText': '\r',
									'text': '\r',
								},
								session_id=cdp_session.session_id,
							)
							await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
								params={
									'type': 'char',
									'windowsVirtualKeyCode': 13,
									'unmodifiedText': '\r',
									'text': '\r',
								},
								session_id=cdp_session.session_id,
							)
							await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
								params={
									'type': 'keyUp',
									'windowsVirtualKeyCode': 13,
									'unmodifiedText': '\r',
									'text': '\r',
								},
								session_id=cdp_session.session_id,
							)
							continue

						# Get proper modifiers and key info for the character
						modifiers, vk_code, base_key = self.character_key_info(char)
						key_code = self.key_code_for_character(base_key)

						# Send keyDown
						await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
							params={
								'type': 'keyDown',
								'key': base_key,
								'code': key_code,
								'modifiers': modifiers,
								'windowsVirtualKeyCode': vk_code,
							},
							session_id=cdp_session.session_id,
						)

						# Send char event with text - this is what makes text appear in input fields
						await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
							params={
								'type': 'char',
								'text': char,
								'key': char,
							},
							session_id=cdp_session.session_id,
						)

						# Send keyUp
						await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
							params={
								'type': 'keyUp',
								'key': base_key,
								'code': key_code,
								'modifiers': modifiers,
								'windowsVirtualKeyCode': vk_code,
							},
							session_id=cdp_session.session_id,
						)

						# Small delay between characters (10ms)
						await asyncio.sleep(0.010)

			self.logger.info(f'⌨️ Sent keys: {keys}')

			# Note: We don't clear cached state on Enter; multi_act will detect DOM changes
			# and rebuild explicitly. We still wait briefly for potential navigation.
			if 'enter' in keys.lower() or 'return' in keys.lower():
				await asyncio.sleep(0.1)
		except Exception as e:
			raise
