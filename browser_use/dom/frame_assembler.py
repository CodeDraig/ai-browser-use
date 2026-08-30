from __future__ import annotations

from typing import TYPE_CHECKING

from cdp_use.cdp.dom.types import Node
from cdp_use.cdp.target import TargetID

from browser_use.dom.tree import DOMRect, EnhancedDOMTreeNode

if TYPE_CHECKING:
	from browser_use.dom.service import DomService

_MIN_CROSS_ORIGIN_IFRAME_EDGE = 10


def is_cross_origin_iframe_size_eligible(width: float, height: float) -> bool:
	"""Include cross-origin frames that are at least 10px in each dimension."""
	return width >= _MIN_CROSS_ORIGIN_IFRAME_EDGE and height >= _MIN_CROSS_ORIGIN_IFRAME_EDGE


class CrossFrameAssembler:
	"""Resolve and attach visible cross-origin frame content to an enhanced tree."""

	def __init__(
		self,
		service: DomService,
		target_id: TargetID,
		iframe_depth: int,
		visited_cross_origin_targets: set[TargetID],
	) -> None:
		self.service = service
		self.target_id = target_id
		self.iframe_depth = iframe_depth
		self.visited_cross_origin_targets = visited_cross_origin_targets

	async def attach(
		self,
		node: Node,
		dom_tree_node: EnhancedDOMTreeNode,
		attributes: dict[str, str] | None,
		total_frame_offset: DOMRect,
		all_frames: dict | None,
	) -> None:
		# handle cross origin iframe (just recursively call the main function with the proper target if it exists in iframes)
		# only do this if the iframe is visible (otherwise it's not worth it)

		if (
			# TODO: hacky way to disable cross origin iframes for now
			self.service.cross_origin_iframes
			and node['nodeName'].upper() == 'IFRAME'
			and node.get('contentDocument', None) is None
		):  # None meaning there is no content
			# Check iframe depth to prevent infinite recursion
			if self.iframe_depth >= self.service.max_iframe_depth:
				self.service.logger.debug(
					f'Skipping iframe at depth {self.iframe_depth} to prevent infinite recursion (max depth: {self.service.max_iframe_depth})'
				)
			else:
				# Ignore only frames smaller than 10px in either dimension.
				should_process_iframe = False

				# First check if the iframe element itself is visible
				if dom_tree_node.is_visible:
					# Check iframe dimensions
					if dom_tree_node.snapshot_node and dom_tree_node.snapshot_node.bounds:
						bounds = dom_tree_node.snapshot_node.bounds
						width = bounds.width
						height = bounds.height

						if is_cross_origin_iframe_size_eligible(width, height):
							should_process_iframe = True
							self.service.logger.debug(
								f'Processing cross-origin iframe: visible=True, width={width}, height={height}'
							)
						else:
							self.service.logger.debug(
								f'Skipping small cross-origin iframe: width={width}, height={height} '
								f'(needs >= {_MIN_CROSS_ORIGIN_IFRAME_EDGE}px per edge)'
							)
					else:
						self.service.logger.debug('Skipping cross-origin iframe: no bounds available')
				else:
					self.service.logger.debug('Skipping invisible cross-origin iframe')

				if should_process_iframe:
					# Lazy fetch all_frames only when actually needed (for cross-origin iframes)
					if all_frames is None:
						all_frames, _ = await self.service.browser_session.session_manager.frames.get_all_frames()
					assert all_frames is not None

					# Use pre-fetched all_frames to find the iframe's target (no redundant CDP call)
					frame_id = node.get('frameId', None)

					# Fallback: if frameId is missing or not in all_frames, try URL matching via
					# the src attribute. This handles dynamically-injected iframes (e.g. HubSpot
					# popups, chat widgets) where Chrome hasn't yet registered the frameId in the
					# frame tree at DOM-snapshot time.
					if (not frame_id or frame_id not in all_frames) and attributes:
						src = attributes.get('src', '')
						if src:
							src_base = src.split('?')[0].rstrip('/')
							for fid, finfo in all_frames.items():
								frame_url = finfo.get('url', '').split('?')[0].rstrip('/')
								if frame_url and frame_url == src_base:
									frame_id = fid
									self.service.logger.debug(f'Matched cross-origin iframe by src URL: {src!r} -> frameId={fid}')
									break

					iframe_document_target = None
					if frame_id:
						frame_info = all_frames.get(frame_id)
						if frame_info and frame_info.get('frameTargetId'):
							iframe_target_id = frame_info['frameTargetId']
							# Use frameTargetId directly from all_frames — get_all_frames() already
							# validated connectivity. Do NOT gate on session_manager.get_target():
							# there is a race where _target_sessions is set (inside the lock in
							# _handle_target_attached) before _targets is populated (outside the
							# lock), so get_target() can transiently return None for a live target.
							iframe_target = self.service.browser_session.session_manager.get_target(iframe_target_id)
							iframe_document_target = {
								'targetId': iframe_target_id,
								'url': iframe_target.url if iframe_target else frame_info.get('url', ''),
								'title': iframe_target.title if iframe_target else frame_info.get('title', ''),
								'type': iframe_target.target_type if iframe_target else 'iframe',
							}

					# if target actually exists in one of the frames, just recursively build the dom tree for it
					if iframe_document_target:
						child_target_id = iframe_document_target['targetId']
						traversed_child_count = len(self.visited_cross_origin_targets) - 1
						if (
							child_target_id in self.visited_cross_origin_targets
							or traversed_child_count >= self.service.max_iframes
						):
							self.service.logger.debug(
								f'Skipping duplicate or over-limit cross-origin iframe target {child_target_id}'
							)
							return

						self.visited_cross_origin_targets.add(child_target_id)
						self.service.logger.debug(
							f'Getting content document for iframe {node.get("frameId", None)} at depth {self.iframe_depth + 1}'
						)
						try:
							content_document, _ = await self.service.get_dom_tree(
								target_id=child_target_id,
								all_frames=all_frames,
								# Current config: if the cross origin iframe is AT ALL visible, include everything inside it
								initial_total_frame_offset=total_frame_offset,
								iframe_depth=self.iframe_depth + 1,
								visited_cross_origin_targets=self.visited_cross_origin_targets,
							)
							dom_tree_node.content_document = content_document
							dom_tree_node.content_document.parent_node = dom_tree_node
						except Exception as e:
							self.service.logger.debug(f'Failed to get DOM tree for cross-origin iframe {frame_id}: {e}')
