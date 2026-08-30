from __future__ import annotations

from typing import TYPE_CHECKING

from cdp_use.cdp.target import TargetID

from browser_use.dom.tree import EnhancedDOMTreeNode

if TYPE_CHECKING:
	from browser_use.browser.session_manager import CDPSession, SessionManager


class FrameResolver:
	"""Own frame discovery and route frame/node operations to their CDP sessions."""

	def __init__(self, manager: SessionManager) -> None:
		self.manager = manager

	async def get_all_frames(self) -> tuple[dict[str, dict], dict[str, str]]:
		"""Get a complete frame hierarchy from all browser targets.

		Returns:
			Tuple of (all_frames, target_sessions) where:
			- all_frames: dict mapping frame_id -> frame info dict with all metadata
			- target_sessions: dict mapping target_id -> session_id for active sessions
		"""
		all_frames = {}  # frame_id -> FrameInfo dict
		target_sessions = {}  # target_id -> session_id (keep sessions alive during collection)

		# Check if cross-origin iframe support is enabled
		include_cross_origin = self.manager.browser_session.browser_profile.cross_origin_iframes

		# Get all targets - only include iframes if cross-origin support is enabled
		targets = await self.manager.get_all_pages(
			include_http=True,
			include_about=True,
			include_pages=True,
			include_iframes=include_cross_origin,  # Only include iframe targets if flag is set
			include_workers=False,
			include_chrome=False,
			include_chrome_extensions=False,
			include_chrome_error=include_cross_origin,  # Only include error pages if cross-origin is enabled
		)
		all_targets = targets

		# First pass: collect frame trees from ALL targets
		for target in all_targets:
			target_id = target['targetId']

			# Skip iframe targets if cross-origin support is disabled
			if not include_cross_origin and target.get('type') == 'iframe':
				continue

			# When cross-origin support is disabled, only process the current target
			if not include_cross_origin:
				# Only process the current focus target
				if (
					self.manager.browser_session.agent_focus_target_id
					and target_id != self.manager.browser_session.agent_focus_target_id
				):
					continue
				# Use the existing agent_focus target's session - use safe API with focus=False
				try:
					cdp_session = await self.manager.browser_session.get_or_create_cdp_session(
						self.manager.browser_session.agent_focus_target_id, focus=False
					)
				except ValueError:
					continue  # Skip if no session available
			else:
				# Get cached session for this target (don't change focus - iterating frames)
				try:
					cdp_session = await self.manager.browser_session.get_or_create_cdp_session(target_id, focus=False)
				except ValueError:
					continue  # Target may have detached between discovery and session creation

			if cdp_session:
				target_sessions[target_id] = cdp_session.session_id

				try:
					# Try to get frame tree (not all target types support this)
					frame_tree_result = await cdp_session.cdp_client.send.Page.getFrameTree(session_id=cdp_session.session_id)

					# Process the frame tree recursively
					def process_frame_tree(node, parent_frame_id=None):
						"""Recursively process frame tree and add to all_frames."""
						frame = node.get('frame', {})
						current_frame_id = frame.get('id')

						if current_frame_id:
							# For iframe targets, check if the frame has a parentId field
							# This indicates it's an OOPIF with a parent in another target
							actual_parent_id = frame.get('parentId') or parent_frame_id

							# Create frame info with all CDP response data plus our additions
							frame_info = {
								**frame,  # Include all original frame data: id, url, parentId, etc.
								'frameTargetId': target_id,  # Target that can access this frame
								'parentFrameId': actual_parent_id,  # Use parentId from frame if available
								'childFrameIds': [],  # Will be populated below
								'isCrossOrigin': False,  # Will be determined based on context
								'isValidTarget': self.manager._is_valid_target(
									target,
									include_http=True,
									include_about=True,
									include_pages=True,
									include_iframes=True,
									include_workers=False,
									include_chrome=False,  # chrome://newtab, chrome://settings, etc. are not valid frames we can control (for sanity reasons)
									include_chrome_extensions=False,  # chrome-extension://
									include_chrome_error=False,  # chrome-error://  (e.g. when iframes fail to load or are blocked by uBlock Origin)
								),
							}

							# Check if frame is cross-origin based on crossOriginIsolatedContextType
							cross_origin_type = frame.get('crossOriginIsolatedContextType')
							if cross_origin_type and cross_origin_type != 'NotIsolated':
								frame_info['isCrossOrigin'] = True

							# For iframe targets, the frame itself is likely cross-origin
							if target.get('type') == 'iframe':
								frame_info['isCrossOrigin'] = True

							# Skip cross-origin frames if support is disabled
							if not include_cross_origin and frame_info.get('isCrossOrigin'):
								return  # Skip this frame and its children

							# Add child frame IDs (note: OOPIFs won't appear here)
							child_frames = node.get('childFrames', [])
							for child in child_frames:
								child_frame = child.get('frame', {})
								child_frame_id = child_frame.get('id')
								if child_frame_id:
									frame_info['childFrameIds'].append(child_frame_id)

							# Store or merge frame info
							if current_frame_id in all_frames:
								# Frame already seen from another target, merge info
								existing = all_frames[current_frame_id]
								# If this is an iframe target, it has direct access to the frame
								if target.get('type') == 'iframe':
									existing['frameTargetId'] = target_id
									existing['isCrossOrigin'] = True
							else:
								all_frames[current_frame_id] = frame_info

							# Process child frames recursively (only if we're not skipping this frame)
							if include_cross_origin or not frame_info.get('isCrossOrigin'):
								for child in child_frames:
									process_frame_tree(child, current_frame_id)

					# Process the entire frame tree
					process_frame_tree(frame_tree_result.get('frameTree', {}))

				except Exception as e:
					# Target doesn't support Page domain or has no frames
					self.manager.logger.debug(f'Failed to get frame tree for target {target_id}: {e}')

		# Second pass: populate backend node IDs and parent target IDs
		# Only do this if cross-origin support is enabled
		if include_cross_origin:
			await self._populate_frame_metadata(all_frames, target_sessions)

		return all_frames, target_sessions

	async def _populate_frame_metadata(self, all_frames: dict[str, dict], target_sessions: dict[str, str]) -> None:
		"""Populate additional frame metadata like backend node IDs and parent target IDs.

		Args:
			all_frames: Frame hierarchy dict to populate
			target_sessions: Active target sessions
		"""
		for frame_id_iter, frame_info in all_frames.items():
			parent_frame_id = frame_info.get('parentFrameId')

			if parent_frame_id and parent_frame_id in all_frames:
				parent_frame_info = all_frames[parent_frame_id]
				parent_target_id = parent_frame_info.get('frameTargetId')

				# Store parent target ID
				frame_info['parentTargetId'] = parent_target_id

				# Try to get backend node ID from parent context
				if parent_target_id in target_sessions:
					assert parent_target_id is not None
					parent_session_id = target_sessions[parent_target_id]
					try:
						# Enable DOM domain
						await self.manager.browser_session.cdp_client.send.DOM.enable(session_id=parent_session_id)

						# Get frame owner info to find backend node ID
						frame_owner = await self.manager.browser_session.cdp_client.send.DOM.getFrameOwner(
							params={'frameId': frame_id_iter}, session_id=parent_session_id
						)

						if frame_owner:
							frame_info['backendNodeId'] = frame_owner.get('backendNodeId')
							frame_info['nodeId'] = frame_owner.get('nodeId')

					except Exception:
						# Frame owner not available (likely cross-origin)
						pass

	async def find_frame_target(self, frame_id: str, all_frames: dict[str, dict] | None = None) -> dict | None:
		"""Find the frame info for a specific frame ID.

		Args:
			frame_id: The frame ID to search for
			all_frames: Optional pre-built frame hierarchy. If None, will call get_all_frames()

		Returns:
			Frame info dict if found, None otherwise
		"""
		if all_frames is None:
			all_frames, _ = await self.get_all_frames()

		return all_frames.get(frame_id)

	async def cdp_client_for_target(self, target_id: TargetID) -> CDPSession:
		return await self.manager.browser_session.get_or_create_cdp_session(target_id, focus=False)

	async def cdp_client_for_frame(self, frame_id: str) -> CDPSession:
		"""Get a CDP client attached to the target containing the specified frame.

		Builds a unified frame hierarchy from all targets to find the correct target
		for any frame, including OOPIFs (Out-of-Process iframes).

		Args:
			frame_id: The frame ID to search for

		Returns:
			Tuple of (cdp_cdp_session, target_id) for the target containing the frame

		Raises:
			ValueError: If the frame is not found in any target
		"""
		# If cross-origin iframes are disabled, just use the main session
		if not self.manager.browser_session.browser_profile.cross_origin_iframes:
			return await self.manager.browser_session.get_or_create_cdp_session()

		# Get complete frame hierarchy
		all_frames, target_sessions = await self.get_all_frames()

		# Find the requested frame
		frame_info = await self.find_frame_target(frame_id, all_frames)

		if frame_info:
			target_id = frame_info.get('frameTargetId')

			if target_id in target_sessions:
				assert target_id is not None
				# Use existing session
				session_id = target_sessions[target_id]
				# Return the client with session attached (don't change focus)
				return await self.manager.browser_session.get_or_create_cdp_session(target_id, focus=False)

		# Frame not found
		raise ValueError(f"Frame with ID '{frame_id}' not found in any target")

	async def cdp_client_for_node(self, node: EnhancedDOMTreeNode) -> CDPSession:
		"""Get CDP client for a specific DOM node based on its frame.

		IMPORTANT: backend_node_id is only valid in the session where the DOM was captured.
		We trust the node's session_id/frame_id/target_id instead of searching all sessions.
		"""

		# Strategy 1: If node has session_id, try to use that exact session (most specific)
		if node.session_id:
			try:
				# Find the CDP session by session_id from SessionManager
				cdp_session = self.manager.get_session(node.session_id)
				if cdp_session:
					# Get target to log URL
					target = self.manager.get_target(cdp_session.target_id)
					target_url = target.url if target else 'detached target'
					self.manager.logger.debug(
						f'✅ Using session from node.session_id for node {node.backend_node_id}: {target_url}'
					)
					return cdp_session
			except Exception as e:
				self.manager.logger.debug(f'Failed to get session by session_id {node.session_id}: {e}')

		# Strategy 2: If node has frame_id, use that frame's session
		if node.frame_id:
			try:
				cdp_session = await self.cdp_client_for_frame(node.frame_id)
				target = self.manager.get_target(cdp_session.target_id)
				target_url = target.url if target else 'detached target'
				self.manager.logger.debug(f'✅ Using session from node.frame_id for node {node.backend_node_id}: {target_url}')
				return cdp_session
			except Exception as e:
				self.manager.logger.debug(f'Failed to get session for frame {node.frame_id}: {e}')

		# Strategy 3: If node has target_id, use that target's session
		if node.target_id:
			try:
				cdp_session = await self.manager.browser_session.get_or_create_cdp_session(target_id=node.target_id, focus=False)
				target = self.manager.get_target(cdp_session.target_id)
				target_url = target.url if target else 'detached target'
				self.manager.logger.debug(f'✅ Using session from node.target_id for node {node.backend_node_id}: {target_url}')
				return cdp_session
			except Exception as e:
				self.manager.logger.debug(f'Failed to get session for target {node.target_id}: {e}')

		# Strategy 4: Fallback to agent_focus_target_id (the page where agent is currently working)
		if self.manager.browser_session.agent_focus_target_id:
			target = self.manager.get_target(self.manager.browser_session.agent_focus_target_id)
			try:
				# Use safe API with focus=False to avoid changing focus
				cdp_session = await self.manager.browser_session.get_or_create_cdp_session(
					self.manager.browser_session.agent_focus_target_id, focus=False
				)
				if target:
					self.manager.logger.warning(
						f'⚠️ Node {node.backend_node_id} has no session/frame/target info. Using agent_focus session: {target.url}'
					)
				return cdp_session
			except ValueError:
				pass  # Fall through to last resort

		# Last resort: use main session
		self.manager.logger.error(
			f'❌ No session info for node {node.backend_node_id} and no agent_focus available. Using main session.'
		)
		return await self.manager.browser_session.get_or_create_cdp_session()
