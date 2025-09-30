from aiogram.fsm.state import StatesGroup, State

class RegStates(StatesGroup):
    waiting_phone = State()
    waiting_name = State()
    waiting_phone_manual = State()

class PhotoStates(StatesGroup):
    waiting_photo = State()

class CaptainStates(StatesGroup):
    waiting_team_name = State()

class BroadcastStates(StatesGroup):
    waiting_message = State()