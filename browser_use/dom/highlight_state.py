from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from browser_use.dom.tree import EnhancedDOMTreeNode

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession
	from browser_use.dom.coordinate_resolver import DOMCoordinateResolver


class DOMHighlightState:
	"""Own temporary DOM and coordinate highlighting interactions."""

	def __init__(self, browser_session: BrowserSession, coordinates: DOMCoordinateResolver) -> None:
		self.browser_session = browser_session
		self.coordinates = coordinates

	async def remove_highlights(self) -> None:
		"""Remove highlights from the page using CDP."""
		if (
			not self.browser_session.browser_profile.highlight_elements
			and not self.browser_session.browser_profile.dom_highlight_elements
		):
			return

		try:
			async with asyncio.timeout(3.0):
				# Get cached session
				cdp_session = await self.browser_session.get_or_create_cdp_session()

				# Remove highlights via JavaScript - be thorough
				script = """
				(function() {
					// Remove all browser-use highlight elements
					const highlights = document.querySelectorAll('[data-browser-use-highlight]');
					console.log('Removing', highlights.length, 'browser-use highlight elements');
					highlights.forEach(el => el.remove());

					// Also remove by ID in case selector missed anything
					const highlightContainer = document.getElementById('browser-use-debug-highlights');
					if (highlightContainer) {
						console.log('Removing highlight container by ID');
						highlightContainer.remove();
					}

					// Final cleanup - remove any orphaned tooltips
					const orphanedTooltips = document.querySelectorAll('[data-browser-use-highlight="tooltip"]');
					orphanedTooltips.forEach(el => el.remove());

					return { removed: highlights.length };
				})();
				"""
				result = await cdp_session.cdp_client.send.Runtime.evaluate(
					params={'expression': script, 'returnByValue': True}, session_id=cdp_session.session_id
				)

				# Log the result for debugging
				if result and 'result' in result and 'value' in result['result']:
					removed_count = result['result']['value'].get('removed', 0)
					self.browser_session.logger.debug(f'Successfully removed {removed_count} highlight elements')
				else:
					self.browser_session.logger.debug('Highlight removal completed')

		except Exception as e:
			self.browser_session.logger.warning(f'Failed to remove highlights: {e}')

	async def highlight_interaction_element(self, node: EnhancedDOMTreeNode) -> None:
		"""Temporarily highlight an element during interaction for user visibility.

		This creates a visual highlight on the browser that shows the user which element
		is being interacted with. The highlight automatically fades after the configured duration.

		Args:
			node: The DOM node to highlight with backend_node_id for coordinate lookup
		"""
		if not self.browser_session.browser_profile.highlight_elements:
			return

		try:
			import json

			cdp_session = await self.browser_session.session_manager.frames.cdp_client_for_node(node)

			# Get current coordinates
			rect = await self.coordinates.get_element_coordinates(node.backend_node_id, cdp_session)

			color = self.browser_session.browser_profile.interaction_highlight_color
			duration_ms = int(self.browser_session.browser_profile.interaction_highlight_duration * 1000)

			if not rect:
				self.browser_session.logger.debug(f'No coordinates found for backend node {node.backend_node_id}')
				return

			# Create animated corner brackets that start offset and animate inward
			script = f"""
			(function() {{
				const rect = {json.dumps({'x': rect.x, 'y': rect.y, 'width': rect.width, 'height': rect.height})};
				const color = {json.dumps(color)};
				const duration = {duration_ms};

				// Scale corner size based on element dimensions to ensure gaps between corners
				const maxCornerSize = 20;
				const minCornerSize = 8;
				const cornerSize = Math.max(
					minCornerSize,
					Math.min(maxCornerSize, Math.min(rect.width, rect.height) * 0.35)
				);
				const borderWidth = 3;
				const startOffset = 10; // Starting offset in pixels
				const finalOffset = -3; // Final position slightly outside the element

				// Get current scroll position
				const scrollX = window.pageXOffset || document.documentElement.scrollLeft || 0;
				const scrollY = window.pageYOffset || document.documentElement.scrollTop || 0;

				// Create container for all corners
				const container = document.createElement('div');
				container.setAttribute('data-browser-use-interaction-highlight', 'true');
				container.style.cssText = `
					position: absolute;
					left: ${{rect.x + scrollX}}px;
					top: ${{rect.y + scrollY}}px;
					width: ${{rect.width}}px;
					height: ${{rect.height}}px;
					pointer-events: none;
					z-index: 2147483647;
				`;

				// Create 4 corner brackets
				const corners = [
					{{ pos: 'top-left', startX: -startOffset, startY: -startOffset, finalX: finalOffset, finalY: finalOffset }},
					{{ pos: 'top-right', startX: startOffset, startY: -startOffset, finalX: -finalOffset, finalY: finalOffset }},
					{{ pos: 'bottom-left', startX: -startOffset, startY: startOffset, finalX: finalOffset, finalY: -finalOffset }},
					{{ pos: 'bottom-right', startX: startOffset, startY: startOffset, finalX: -finalOffset, finalY: -finalOffset }}
				];

				corners.forEach(corner => {{
					const bracket = document.createElement('div');
					bracket.style.cssText = `
						position: absolute;
						width: ${{cornerSize}}px;
						height: ${{cornerSize}}px;
						pointer-events: none;
						transition: all 0.15s ease-out;
					`;

					// Position corners
					if (corner.pos === 'top-left') {{
						bracket.style.top = '0';
						bracket.style.left = '0';
						bracket.style.borderTop = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.borderLeft = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.transform = `translate(${{corner.startX}}px, ${{corner.startY}}px)`;
					}} else if (corner.pos === 'top-right') {{
						bracket.style.top = '0';
						bracket.style.right = '0';
						bracket.style.borderTop = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.borderRight = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.transform = `translate(${{corner.startX}}px, ${{corner.startY}}px)`;
					}} else if (corner.pos === 'bottom-left') {{
						bracket.style.bottom = '0';
						bracket.style.left = '0';
						bracket.style.borderBottom = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.borderLeft = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.transform = `translate(${{corner.startX}}px, ${{corner.startY}}px)`;
					}} else if (corner.pos === 'bottom-right') {{
						bracket.style.bottom = '0';
						bracket.style.right = '0';
						bracket.style.borderBottom = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.borderRight = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.transform = `translate(${{corner.startX}}px, ${{corner.startY}}px)`;
					}}

					container.appendChild(bracket);

					// Animate to final position slightly outside the element
					setTimeout(() => {{
						bracket.style.transform = `translate(${{corner.finalX}}px, ${{corner.finalY}}px)`;
					}}, 10);
				}});

				document.body.appendChild(container);

				// Auto-remove after duration
				setTimeout(() => {{
					container.style.opacity = '0';
					container.style.transition = 'opacity 0.3s ease-out';
					setTimeout(() => container.remove(), 300);
				}}, duration);

				return {{ created: true }};
			}})();
			"""

			# Fire and forget - don't wait for completion

			await cdp_session.cdp_client.send.Runtime.evaluate(
				params={'expression': script, 'returnByValue': True}, session_id=cdp_session.session_id
			)

		except Exception as e:
			# Don't fail the action if highlighting fails
			self.browser_session.logger.debug(f'Failed to highlight interaction element: {e}')

	async def highlight_coordinate_click(self, x: int, y: int) -> None:
		"""Temporarily highlight a coordinate click position for user visibility.

		This creates a visual highlight at the specified coordinates showing where
		the click action occurred. The highlight automatically fades after the configured duration.

		Args:
			x: Horizontal coordinate relative to viewport left edge
			y: Vertical coordinate relative to viewport top edge
		"""
		if not self.browser_session.browser_profile.highlight_elements:
			return

		try:
			import json

			cdp_session = await self.browser_session.get_or_create_cdp_session()

			color = self.browser_session.browser_profile.interaction_highlight_color
			duration_ms = int(self.browser_session.browser_profile.interaction_highlight_duration * 1000)

			# Create animated crosshair and circle at the click coordinates
			script = f"""
			(function() {{
				const x = {x};
				const y = {y};
				const color = {json.dumps(color)};
				const duration = {duration_ms};

				// Get current scroll position
				const scrollX = window.pageXOffset || document.documentElement.scrollLeft || 0;
				const scrollY = window.pageYOffset || document.documentElement.scrollTop || 0;

				// Create container
				const container = document.createElement('div');
				container.setAttribute('data-browser-use-coordinate-highlight', 'true');
				container.style.cssText = `
					position: absolute;
					left: ${{x + scrollX}}px;
					top: ${{y + scrollY}}px;
					width: 0;
					height: 0;
					pointer-events: none;
					z-index: 2147483647;
				`;

				// Create outer circle
				const outerCircle = document.createElement('div');
				outerCircle.style.cssText = `
					position: absolute;
					left: -15px;
					top: -15px;
					width: 30px;
					height: 30px;
					border: 3px solid ${{color}};
					border-radius: 50%;
					opacity: 0;
					transform: scale(0.3);
					transition: all 0.2s ease-out;
				`;
				container.appendChild(outerCircle);

				// Create center dot
				const centerDot = document.createElement('div');
				centerDot.style.cssText = `
					position: absolute;
					left: -4px;
					top: -4px;
					width: 8px;
					height: 8px;
					background: ${{color}};
					border-radius: 50%;
					opacity: 0;
					transform: scale(0);
					transition: all 0.15s ease-out;
				`;
				container.appendChild(centerDot);

				document.body.appendChild(container);

				// Animate in
				setTimeout(() => {{
					outerCircle.style.opacity = '0.8';
					outerCircle.style.transform = 'scale(1)';
					centerDot.style.opacity = '1';
					centerDot.style.transform = 'scale(1)';
				}}, 10);

				// Animate out and remove
				setTimeout(() => {{
					outerCircle.style.opacity = '0';
					outerCircle.style.transform = 'scale(1.5)';
					centerDot.style.opacity = '0';
					setTimeout(() => container.remove(), 300);
				}}, duration);

				return {{ created: true }};
			}})();
			"""

			# Fire and forget - don't wait for completion
			await cdp_session.cdp_client.send.Runtime.evaluate(
				params={'expression': script, 'returnByValue': True}, session_id=cdp_session.session_id
			)

		except Exception as e:
			# Don't fail the action if highlighting fails
			self.browser_session.logger.debug(f'Failed to highlight coordinate click: {e}')

	async def add_highlights(self, selector_map: dict[int, EnhancedDOMTreeNode]) -> None:
		"""Add visual highlights to the browser DOM for user visibility."""
		if not self.browser_session.browser_profile.dom_highlight_elements or not selector_map:
			return

		try:
			import json

			# Convert selector_map to the format expected by the highlighting script
			elements_data = []
			for element_index, node in selector_map.items():
				# Get bounding box using absolute position (includes iframe translations) if available
				if node.absolute_position:
					# Use absolute position which includes iframe coordinate translations
					rect = node.absolute_position
					bbox = {'x': rect.x, 'y': rect.y, 'width': rect.width, 'height': rect.height}

					# Only include elements with valid bounding boxes
					if bbox and bbox.get('width', 0) > 0 and bbox.get('height', 0) > 0:
						element = {
							'x': bbox['x'],
							'y': bbox['y'],
							'width': bbox['width'],
							'height': bbox['height'],
							'element_name': node.node_name,
							'is_clickable': node.snapshot_node.is_clickable if node.snapshot_node else True,
							'is_scrollable': getattr(node, 'is_scrollable', False),
							'attributes': node.attributes or {},
							'frame_id': getattr(node, 'frame_id', None),
							'node_id': node.node_id,
							'backend_node_id': node.backend_node_id,
							'element_index': element_index,
							'xpath': node.xpath,
							'text_content': node.get_all_children_text()[:50]
							if hasattr(node, 'get_all_children_text')
							else node.node_value[:50],
						}
						elements_data.append(element)

			if not elements_data:
				self.browser_session.logger.debug('⚠️ No valid elements to highlight')
				return

			self.browser_session.logger.debug(f'📍 Creating highlights for {len(elements_data)} elements')

			# Always remove existing highlights first
			await self.remove_highlights()

			# Add a small delay to ensure removal completes
			import asyncio

			await asyncio.sleep(0.05)

			# Get CDP session
			cdp_session = await self.browser_session.get_or_create_cdp_session()

			# Create the proven highlighting script from v0.6.0 with fixed positioning
			script = f"""
			(function() {{
				// Interactive elements data
				const interactiveElements = {json.dumps(elements_data)};

				console.log('=== BROWSER-USE HIGHLIGHTING ===');
				console.log('Highlighting', interactiveElements.length, 'interactive elements');

				// Double-check: Remove any existing highlight container first
				const existingContainer = document.getElementById('browser-use-debug-highlights');
				if (existingContainer) {{
					console.log('⚠️ Found existing highlight container, removing it first');
					existingContainer.remove();
				}}

				// Also remove any stray highlight elements
				const strayHighlights = document.querySelectorAll('[data-browser-use-highlight]');
				if (strayHighlights.length > 0) {{
					console.log('⚠️ Found', strayHighlights.length, 'stray highlight elements, removing them');
					strayHighlights.forEach(el => el.remove());
				}}

				// Use maximum z-index for visibility
				const HIGHLIGHT_Z_INDEX = 2147483647;

				// Create container for all highlights - use FIXED positioning (key insight from v0.6.0)
				const container = document.createElement('div');
				container.id = 'browser-use-debug-highlights';
				container.setAttribute('data-browser-use-highlight', 'container');

				container.style.cssText = `
					position: absolute;
					top: 0;
					left: 0;
					width: 100vw;
					height: 100vh;
					pointer-events: none;
					z-index: ${{HIGHLIGHT_Z_INDEX}};
					overflow: visible;
					margin: 0;
					padding: 0;
					border: none;
					outline: none;
					box-shadow: none;
					background: none;
					font-family: inherit;
				`;

				// Helper function to create text elements safely
				function createTextElement(tag, text, styles) {{
					const element = document.createElement(tag);
					element.textContent = text;
					if (styles) element.style.cssText = styles;
					return element;
				}}

				// Add highlights for each element
				interactiveElements.forEach((element, index) => {{
					const highlight = document.createElement('div');
					highlight.setAttribute('data-browser-use-highlight', 'element');
					highlight.setAttribute('data-element-id', element.element_index);
					highlight.style.cssText = `
						position: absolute;
						left: ${{element.x}}px;
						top: ${{element.y}}px;
						width: ${{element.width}}px;
						height: ${{element.height}}px;
						outline: 2px dashed #4a90e2;
						outline-offset: -2px;
						background: transparent;
						pointer-events: none;
						box-sizing: content-box;
						transition: outline 0.2s ease;
						margin: 0;
						padding: 0;
						border: none;
					`;

					// Label with the same selector index shown to the model
					const label = createTextElement('div', element.element_index, `
						position: absolute;
						top: -20px;
						left: 0;
						background-color: #4a90e2;
						color: white;
						padding: 2px 6px;
						font-size: 11px;
						font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
						font-weight: bold;
						border-radius: 3px;
						white-space: nowrap;
						z-index: ${{HIGHLIGHT_Z_INDEX + 1}};
						box-shadow: 0 2px 4px rgba(0,0,0,0.3);
						border: none;
						outline: none;
						margin: 0;
						line-height: 1.2;
					`);

					highlight.appendChild(label);
					container.appendChild(highlight);
				}});

				// Add container to document
				document.body.appendChild(container);

				console.log('Highlighting complete - added', interactiveElements.length, 'highlights');
				return {{ added: interactiveElements.length }};
			}})();
			"""

			# Execute the script
			result = await cdp_session.cdp_client.send.Runtime.evaluate(
				params={'expression': script, 'returnByValue': True}, session_id=cdp_session.session_id
			)

			# Log the result
			if result and 'result' in result and 'value' in result['result']:
				added_count = result['result']['value'].get('added', 0)
				self.browser_session.logger.debug(f'Successfully added {added_count} highlight elements to browser DOM')
			else:
				self.browser_session.logger.debug('Browser highlight injection completed')

		except Exception as e:
			self.browser_session.logger.warning(f'Failed to add browser highlights: {e}')
			import traceback

			self.browser_session.logger.debug(f'Browser highlight traceback: {traceback.format_exc()}')
