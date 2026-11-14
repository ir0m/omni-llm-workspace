"""Explicit native session changes. Callers serialize these with discovery and sends."""
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select

from .agents.session_history import text_content
from .external_sessions import family, origin, same_origin
from .models import AgentSession, ExternalSession, Message
from .projects import project_path


def sources(state, session, conversation):
    result, seen = [], set()
    for link in conversation.agent_sessions:
        key = (family(link.agent), link.native_id)
        if key[0] not in ("codex", "claude") or link.ephemeral or key in seen:
            continue
        seen.add(key)
        if not state.settings.omni_allow_native_session_changes:
            raise HTTPException(403, "CLI側セッションの変更は設定で無効です")
        agent = state.agents.get(link.agent)
        if not agent or not agent.installed():
            raise HTTPException(409, "CLI側の実行元が無効です。設定を確認してください")
        external = session.scalar(select(ExternalSession).where(ExternalSession.family == key[0], ExternalSession.native_id == key[1]))
        cwd = (project_path(state.settings.omni_projects_dir, conversation.project) if conversation.project
               else state.settings.omni_data_dir / "agent-chat")
        if external:
            cwd = project_path(state.settings.omni_projects_dir, external.project)
            if str(cwd.resolve()) != external.cwd:
                raise HTTPException(409, "元のプロジェクトフォルダが変わっています")
        result.append((link, agent, cwd, external))
    return result


async def histories(refs):
    found = []
    for link, agent, cwd, external in refs:
        turns = await agent.session_history(link.native_id, cwd)
        if any(turn.status == "streaming" for turn in turns):
            raise HTTPException(409, "CLI側で実行中です。終了後に操作してください")
        found.append(turns)
    return found


async def rename(state, session, conversation, title):
    refs = sources(state, session, conversation)
    await histories(refs)
    for link, agent, cwd, _ in refs:
        await agent.rename_session(link.native_id, cwd, title or "無題")


async def delete(state, session, conversation):
    refs = sources(state, session, conversation)
    await histories(refs)
    for link, agent, cwd, external in refs:
        await agent.delete_session(link.native_id, cwd)
        # Save successful deletions independently so a later failure remains retryable.
        if external:
            external.conversation_id = None
        for sibling in list(conversation.agent_sessions):
            if family(sibling.agent) == family(link.agent) and sibling.native_id == link.native_id:
                conversation.agent_sessions.remove(sibling)
        session.commit()


async def fork(state, session, source, copy, messages, copies):
    refs = sources(state, session, source)
    all_turns = await histories(refs)
    # Resolve every boundary before creating anything. Never silently fork later context.
    plans = []
    for ref, turns in zip(refs, all_turns):
        link, agent, cwd, external = ref
        expected = project_path(state.settings.omni_projects_dir, copy.project) if copy.project else state.settings.omni_data_dir / "agent-chat"
        if cwd.resolve() != expected.resolve():
            raise HTTPException(409, "CLIセッションと現在のプロジェクトが異なります。元のプロジェクトに戻してから分岐してください")
        relevant = [m for m in messages if m.role == "assistant" and family(m.agent or m.provider or "") == family(link.agent)]
        if not relevant:
            continue
        last = relevant[-1]
        mapping = external.turns if external else {}
        turn_id = next((key for key, ids in mapping.items() if ids.get("assistant") == last.id), None)
        if not turn_id and same_origin(last.native_origin, link.agent, link.native_id):
            turn_id = last.native_origin.get("turn")
        matches = [turn for turn in turns if turn_id in [turn.id, *turn.source_ids]] if turn_id else [
            turn for turn in turns if text_content(turn.assistant) and text_content(turn.assistant) == text_content(last.content)]
        if len(matches) != 1:
            raise HTTPException(409, "CLI履歴の分岐位置を一意に特定できません。会話を更新してから再度お試しください")
        boundary = matches[0]
        cutoff = boundary.id if family(link.agent) == "codex" else (boundary.source_ids[-1] if boundary.source_ids else boundary.id)
        plans.append((ref, turns[:turns.index(boundary) + 1], cutoff, last.seq))
    made = []
    try:
        for (link, agent, cwd, external), previous, cutoff, seq in plans:
            new_id = await agent.fork_session(link.native_id, cwd, copy.title, cutoff)
            made.append((agent, new_id, cwd))
            new_turns = await agent.session_history(new_id, cwd)
            if len(new_turns) != len(previous):
                raise HTTPException(409, "CLI側の分岐位置を検証できませんでした")
            session.add(AgentSession(conversation_id=copy.id, agent=link.agent, native_id=new_id, synced_seq=seq))
            mapping = {}
            for old, new in zip(previous, new_turns):
                if old.user != new.user or old.assistant != new.assistant:
                    raise HTTPException(409, "CLI側の分岐履歴が一致しませんでした")
                # Suppress reimport even for injected context with no one-to-one local message.
                ids = external.turns.get(old.id, {}) if external else {}
                mapping[new.id] = {"user": copies.get(ids.get("user"), ""), "assistant": copies.get(ids.get("assistant"), ""), "owned": True}
                for m in messages:
                    if same_origin(m.native_origin, link.agent, link.native_id) and m.native_origin.get("turn") in [old.id, *old.source_ids]:
                        session.get(Message, copies[m.id]).native_origin = {**origin(link.agent, new_id), "turn": new.id}
            if source.project:
                session.add(ExternalSession(family=family(link.agent), agent=link.agent, native_id=new_id,
                    conversation_id=copy.id, project=source.project, cwd=str(cwd.resolve()), turns=mapping, pending=[]))
            for m in messages:
                if m.seq <= seq and m.native_origin == origin(link.agent, link.native_id):
                    # Only originals already known to this native session should skip future handover.
                    session.get(Message, copies[m.id]).native_origin = origin(link.agent, new_id)
        return made
    except Exception:
        await discard(made)
        raise


async def discard(made):
    for agent, native_id, cwd in made:
        try:
            await agent.delete_session(native_id, cwd)
        except Exception:
            # A failed cleanup must be visible: the user can find and remove the orphan via discovery.
            raise HTTPException(502, "分岐に失敗し、CLI側に作成した会話が残っています。一覧を更新して確認してください")
