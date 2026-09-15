import os
import re
import json
import asyncio
import requests
from dotenv import load_dotenv
from agents import SlackAgent, KnowledgeAgent, SearchAgent, CalendarAgent, CommunicationAgent
import dateparser
from datetime import datetime, timedelta
import logging

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
)

PLANNER_PROMPT_TEMPLATE = """
You are an expert planning agent. Your job is to create a plan to fulfill a user's request.
Here are the available agents and their EXACT action formats:
- "KnowledgeAgent":
* To add knowledge: "Add knowledge: 'content here' in filename"
* To query: "What is [question]?"
- "SearchAgent": "Search for [query]"
- "SlackAgent": "Post \"message text\" to #channel"
- "CalendarAgent": "Schedule [event name] for [time]"
- "CommunicationAgent": "Send SMS to [number]: [message]"
CRITICAL: Use the exact formats shown above. For SlackAgent, always use: Post "message"
to #channel
Respond with ONLY a JSON array of steps (no backticks or extra text).
Each step must have "agent" and "action" fields.
User Request: "{user_prompt}"
"""

class TaskOrchestrator:
    def __init__(self, task_id: str, prompt: str, ws_manager):
        self.task_id = task_id
        self.prompt = prompt
        self.ws_manager = ws_manager
        self.knowledge_agent = KnowledgeAgent()
        self.search_agent = SearchAgent()
        self.calendar_agent = CalendarAgent()
        self.communication_agent = CommunicationAgent()

        slack_token = os.getenv("SLACK_BOT_TOKEN")
        if not slack_token:
            raise RuntimeError("Missing SLACK_BOT_TOKEN")
        self.slack_agent = SlackAgent(token=slack_token)

        self.plan = []
        self.context = {}

    async def _gemini_request(self, data: dict, template: str, is_json=True):
        if not GEMINI_API_KEY:
            raise RuntimeError("Missing GEMINI_API_KEY")
        payload = {"contents": [{"parts": [{"text": template.format(**data)}]}]}
        r = requests.post(GEMINI_API_URL, json=payload, timeout=60)
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        if is_json:
            clean = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            clean = re.sub(r"\s*```$", "", clean).strip()
            return json.loads(clean)
        return text

    async def execute_plan(self):
        m = re.search(
            r"Post a message on\s+(#[^\s]+)\s+channel in Slack saying ['\"](.+?)['\"]",
            self.prompt,
            flags=re.IGNORECASE
        )
        if m:
            channel, msg = m.group(1), m.group(2)
            action = f'Post "{msg}" to {channel}'
            self.plan = [{"agent": "SlackAgent", "action": action}]
        else:
            await self.ws_manager.broadcast(json.dumps({
                "type": "log",
                "agent": "PlannerAgent",
                "message": "Generating plan...",
                "log_type": "info"
            }))
            try:
                plan = await self._gemini_request({"user_prompt": self.prompt},
                                                   PLANNER_PROMPT_TEMPLATE)
                self.plan = plan if isinstance(plan, list) else [plan]
            except Exception as e:
                await self.ws_manager.broadcast(json.dumps({
                    "type": "log",
                    "agent": "System",
                    "message": f"Plan error: {e}",
                    "log_type": "error"
                }))
                return

        await self.ws_manager.broadcast(json.dumps({"type": "plan", "steps": self.plan}))
        for step in self.plan:
            await asyncio.sleep(1)
            await self._execute_step(step)

        await self.ws_manager.broadcast(json.dumps({
            "type": "log",
            "agent": "System",
            "message": "Task completed",
            "log_type": "success"
        }))

    async def _execute_step(self, step):
        agent, action = step["agent"], step["action"]
        await self.ws_manager.broadcast(json.dumps({
            "type": "status_update", "step_action": action, "status": "in-progress"
        }))
        await self.ws_manager.broadcast(json.dumps({
            "type": "log", "agent": agent, "message": f"Starting: {action}", "log_type": "info"
        }))
        try:
            if agent == "KnowledgeAgent":
                # Handle both add and query operations
                if action.lower().startswith("add knowledge"):
                    # Parse: "Add knowledge: 'content' in filename"
                    import re
                    match = re.search(r"add knowledge:\s*['\"](.+?)['\"](?:.*in\s+(\w+))?", action, flags=re.IGNORECASE)
                    if match:
                        content, filename = match.groups()
                        resp = await self.knowledge_agent.add_knowledge(filename, content)
                        msg = resp
                    else:
                        raise ValueError(f"Could not parse knowledge add action: {action}")
                else:
                    # Regular query
                    resp = await self.knowledge_agent.run(action)
                    msg = "Knowledge query completed"
            elif agent == "SlackAgent":
                resp = await self.slack_agent.execute(action)
                msg = "Slack message posted successfully"
            elif agent == "CalendarAgent":
                # Parse calendar action and create event
                event_details = self._parse_calendar_action(action)
                resp = await self.calendar_agent.run(event_details)
                msg = f"Calendar event created: {resp}"
            else:
                await asyncio.sleep(1)
                msg = f"Executed {agent}"

            status = "completed"
            lt = "success"
        except Exception as e:
            msg = f"Error: {e}"
            status = "failed"
            lt = "error"

        await self.ws_manager.broadcast(json.dumps({
            "type": "status_update", "step_action": action, "status": status
        }))
        await self.ws_manager.broadcast(json.dumps({
            "type": "log", "agent": agent, "message": msg, "log_type": lt
        }))

    def _parse_calendar_action(self, action):
        """Parse calendar action to extract event details from natural language using dateparser"""
        import logging
        # Regex to find the first 'for', 'at', or 'on' followed by a date/time-like phrase
        # (e.g., a number, month name, or day name)
        date_time_pattern = r'\b(for|at|on)\b\s+(?=(\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|monday|tuesday|wednesday|thursday|friday|saturday|sunday))'
        match = re.search(date_time_pattern, action, re.IGNORECASE)
        if match:
            split_idx = match.start()
            title = action[:split_idx].strip()
            time_str = action[split_idx + len(match.group(0)):].strip(' .')
        else:
            # Fallback: try to extract after 'schedule'
            title_match = re.search(r'schedule\s+(.*?)(?:\s+for|\s+at|\s+on|$)', action, re.IGNORECASE)
            title = title_match.group(1).strip(' .') if title_match else "Meeting"
            time_str = None

        if not title:
            title = "Meeting"

        print(f"[Calendar NLP] Extracted title: {title}")
        print(f"[Calendar NLP] Extracted time_str: {time_str}")
        event_time = None
        if time_str:
            event_time = dateparser.parse(time_str)
            print(f"[Calendar NLP] dateparser.parse(time_str): {event_time}")
        if not event_time:
            event_time = dateparser.parse(action)
            print(f"[Calendar NLP] dateparser.parse(action): {event_time}")
        if not event_time:
            event_time = datetime.now() + timedelta(days=1)
            event_time = event_time.replace(hour=10, minute=0, second=0, microsecond=0)
            print(f"[Calendar NLP] Fallback event_time: {event_time}")

        end_time = event_time + timedelta(hours=1)

        return {
            "title": title,
            "start_time": event_time.isoformat(),
            "end_time": end_time.isoformat()
        }
