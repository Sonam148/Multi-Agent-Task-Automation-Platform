import os
import os.path
import re
from slack_sdk import WebClient
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from twilio.rest import Client
from duckduckgo_search import DDGS
import requests

# These variables will be populated from .env by main.py
TWILIO_ACCOUNT_SID = ""
TWILIO_AUTH_TOKEN = ""
TWILIO_PHONE_NUMBER = ""
SLACK_BOT_TOKEN = ""
GEMINI_API_KEY = ""

# --- THIS IS THE NEW, MORE ROBUST PATH LOGIC ---
# Get the absolute path of the directory where this agents.py file is located
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Join this directory path with the credentials filename
CREDENTIALS_PATH = os.path.join(BASE_DIR, 'credentials.json')
TOKEN_PATH = os.path.join(BASE_DIR, 'token.json')

class SlackAgent:
    def __init__(self, token: str):
        self.client = AsyncWebClient(token=token)

    async def execute(self, action: str):
        print("🔷 SlackAgent.execute got action:", action)
        m = re.match(r'^Post\s+"(.+)"\s+to\s+(#[^\s]+)$', action, flags=re.IGNORECASE)
        if not m:
            m = re.match(r"post_message\(channel='(#[^']+)', message='(.+)'\)", action, flags=re.IGNORECASE)
            if m:
                channel, msg = m.groups()
            else:
                raise ValueError(f"Could not parse Slack action: {action!r}")
        else:
            msg, channel = m.groups()
        resp = await self.client.chat_postMessage(channel=channel, text=msg)
        return resp

class KnowledgeAgent:
    def __init__(self, directory="knowledge_base"):
        self.directory = os.path.join(BASE_DIR, directory)
        os.makedirs(self.directory, exist_ok=True)  # Ensure directory exists
        self.knowledge = self._load_knowledge()

    def _load_knowledge(self):
        if not os.path.exists(self.directory):
            os.makedirs(self.directory)
        full_text = ""
        for filename in os.listdir(self.directory):
            if filename.endswith(".txt"):
                with open(os.path.join(self.directory, filename), 'r', encoding="utf-8") as f:
                    full_text += f.read() + "\n\n"
        return full_text

    async def add_knowledge(self, filename: str, content: str) -> str:
        """Add knowledge to a file in the knowledge_base folder"""
        import re
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', filename)
        file_path = os.path.join(self.directory, safe_name + ".txt")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content.strip() + "\n")
        self.knowledge = self._load_knowledge()
        return f"Knowledge successfully stored in {safe_name}.txt"

    async def run(self, query: str) -> str:
        if not self.knowledge:
            return "Knowledge base is empty."
        prompt_template = "Context: {context}\n\nQuestion: {question}\n\nAnswer based only on the context:"
        final_prompt = prompt_template.format(context=self.knowledge, question=query)
        headers = {"Content-Type": "application/json"}
        payload = {"contents": [{"parts": [{"text": final_prompt}]}]}
        gemini_url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?"
            f"key={GEMINI_API_KEY}"
        )
        try:
            response = requests.post(gemini_url, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            return response.json()['candidates'][0]['content']['parts'][0]['text'].strip()
        except Exception as e:
            return f"Error consulting knowledge base: {e}"

class SearchAgent:
    async def run(self, query: str) -> str:
        try:
            with DDGS() as ddgs:
                results = [r for r in ddgs.text(query, max_results=3)]
                if not results:
                    return "No results found."
                return "\n".join([f"{r['title']}: {r['body']}" for r in results])
        except Exception as e:
            return f"Error during search: {e}"

SCOPES = ["https://www.googleapis.com/auth/calendar"]

class CalendarAgent:
    def __init__(self):
        self.creds = self._get_credentials()

    def _get_credentials(self):
        creds = None
        if os.path.exists(TOKEN_PATH):
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(CREDENTIALS_PATH):
                    raise FileNotFoundError(f"FATAL: credentials.json not found at the expected path: {CREDENTIALS_PATH}. Please ensure the file exists and is named correctly.")
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
                creds = flow.run_local_server(port=0)
                with open(TOKEN_PATH, "w") as token:
                    token.write(creds.to_json())
        return creds

    async def run(self, event_details: dict):
        try:
            service = build("calendar", "v3", credentials=self.creds)
            event = {
                "summary": event_details.get("title"),
                "start": {"dateTime": event_details["start_time"], "timeZone": "Asia/Kolkata"},
                "end": {"dateTime": event_details["end_time"], "timeZone": "Asia/Kolkata"}
            }
            event = service.events().insert(calendarId="primary", body=event).execute()
            return event.get('htmlLink')
        except HttpError as error:
            raise Exception(f"Google Calendar API Error: {error}")

class CommunicationAgent:
    def __init__(self):
        if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
            self.client = None
            print("WARNING: Twilio credentials not set in .env file. CommunicationAgent will not work.")
        else:
            self.client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    def send_sms(self, recipient: str, message: str) -> str:
        if not self.client:
            raise Exception("Twilio client not initialized.")
        message_obj = self.client.messages.create(body=message, from_=TWILIO_PHONE_NUMBER, to=recipient)
        return message_obj.sid

    def make_call(self, recipient: str, message: str) -> str:
        if not self.client:
            raise Exception("Twilio client not initialized.")
        twiml_message = f'<Response><Say>{message}</Say></Response>'
        call = self.client.calls.create(twiml=twiml_message, to=recipient, from_=TWILIO_PHONE_NUMBER)
        return call.sid
