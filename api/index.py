"""
Roza Tracker — FastAPI Backend
Ramadan Food Expense Splitter API
"""

import os
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()   

# ---------------------------------------------------------------------------
# Supabase Client
# ---------------------------------------------------------------------------
SUPABASE_URL: str = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY: str = os.environ.get("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "Missing SUPABASE_URL or SUPABASE_KEY environment variables. "
        "Set them in your Vercel project settings or in a local .env file."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Roza Tracker API",
    description="Track and split Ramadan food expenses",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class ExpenseCreate(BaseModel):
    buyer_id: str = Field(..., min_length=1, description="UUID of the buyer")
    amount: float = Field(..., description="Expense amount")
    description: str = Field("", description="What was purchased")
    split_among: list[str] = Field(default_factory=list, description="List of UUIDs who share this expense")
    is_settlement: bool = Field(False, description="Is this a peer-to-peer settlement?")


class SettlementCreate(BaseModel):
    payer_id: str = Field(..., min_length=1, description="UUID of the user paying")
    payee_id: str = Field(..., min_length=1, description="UUID of the user receiving")
    amount: float = Field(..., gt=0, description="Amount transferred")


class ExpenseUpdate(BaseModel):
    buyer_id: Optional[str] = Field(None, min_length=1, description="UUID of the buyer")
    amount: Optional[float] = Field(None, description="Expense amount")
    description: Optional[str] = Field(None, description="What was purchased")
    split_among: Optional[list[str]] = Field(None, description="List of UUIDs who share this expense")


class ExpenseOut(BaseModel):
    id: str
    buyer_id: str
    buyer_name: Optional[str] = None
    amount: float
    description: str
    created_at: str
    split_among: Optional[list[str]] = None
    is_settlement: Optional[bool] = False


class UserOut(BaseModel):
    id: str
    name: str


class BalanceItem(BaseModel):
    id: str
    name: str
    total_paid: float
    balance: float
    status: str  # "Owed" or "Owes"


class SummaryOut(BaseModel):
    total_spent: float
    per_head: float
    user_count: int
    balances: list[BalanceItem]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/users", response_model=list[UserOut])
async def get_users():
    """Return all users."""
    response = supabase.table("users").select("id, name").order("name").execute()
    return response.data


@app.get("/api/expenses", response_model=list[ExpenseOut])
async def get_expenses():
    """Return all expenses, most recent first, with buyer name joined."""
    response = (
        supabase.table("expenses")
        .select("id, buyer_id, amount, description, created_at, split_among, is_settlement, users(name)")
        .order("created_at", desc=True)
        .execute()
    )

    results = []
    for row in response.data:
        buyer_name = ""
        if row.get("users") and isinstance(row["users"], dict):
            buyer_name = row["users"].get("name", "")
        results.append(
            ExpenseOut(
                id=row["id"],
                buyer_id=row["buyer_id"],
                buyer_name=buyer_name,
                amount=row["amount"],
                description=row.get("description", ""),
                created_at=row["created_at"],
                split_among=row.get("split_among", []),
                is_settlement=row.get("is_settlement", False),
            )
        )
    return results


@app.post("/api/expenses", response_model=ExpenseOut, status_code=201)
async def add_expense(expense: ExpenseCreate):
    """Add a new expense."""
    payload = {
        "buyer_id": expense.buyer_id,
        "amount": expense.amount,
        "description": expense.description,
        "split_among": expense.split_among,
        "is_settlement": expense.is_settlement,
    }
    response = supabase.table("expenses").insert(payload).execute()

    if not response.data:
        raise HTTPException(status_code=500, detail="Failed to insert expense")

    row = response.data[0]

    # Fetch buyer name
    user_resp = (
        supabase.table("users")
        .select("name")
        .eq("id", expense.buyer_id)
        .single()
        .execute()
    )
    buyer_name = user_resp.data.get("name", "") if user_resp.data else ""

    return ExpenseOut(
        id=row["id"],
        buyer_id=row["buyer_id"],
        buyer_name=buyer_name,
        amount=row["amount"],
        description=row.get("description", ""),
        created_at=row["created_at"],
    )


@app.post("/api/settle", status_code=201)
async def settle_debt(settlement: SettlementCreate):
    """Settle debt between two users without affecting the group's net spend."""
    if settlement.payer_id == settlement.payee_id:
        raise HTTPException(status_code=400, detail="Payer and payee cannot be the same")

    # Fetch names
    users_resp = (
        supabase.table("users")
        .select("id, name")
        .in_("id", [settlement.payer_id, settlement.payee_id])
        .execute()
    )
    users_dict = {u["id"]: u["name"] for u in (users_resp.data or [])}

    payer_name = users_dict.get(settlement.payer_id, "Unknown")
    payee_name = users_dict.get(settlement.payee_id, "Unknown")

    # Record settlement as a single entry using new ledger schema
    payload = {
        "buyer_id": settlement.payer_id,
        "amount": settlement.amount,
        "description": f"Paid {payee_name} to settle up",
        "split_among": [settlement.payee_id],
        "is_settlement": True,
    }

    response = supabase.table("expenses").insert(payload).execute()

    if not response.data:
        raise HTTPException(status_code=500, detail="Failed to record settlement")

    return {"message": "Settlement recorded successfully"}


@app.put("/api/expenses/{expense_id}", response_model=ExpenseOut)
async def update_expense(expense_id: str, expense: ExpenseUpdate):
    """Update an existing expense."""
    update_data = {k: v for k, v in expense.dict().items() if v is not None}
    
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    response = (
        supabase.table("expenses")
        .update(update_data)
        .eq("id", expense_id)
        .execute()
    )

    if not response.data:
        raise HTTPException(status_code=404, detail="Expense not found or update failed")

    row = response.data[0]

    # Fetch buyer name
    user_resp = (
        supabase.table("users")
        .select("name")
        .eq("id", row["buyer_id"])
        .single()
        .execute()
    )
    buyer_name = user_resp.data.get("name", "") if user_resp.data else ""

    return ExpenseOut(
        id=row["id"],
        buyer_id=row["buyer_id"],
        buyer_name=buyer_name,
        amount=row["amount"],
        description=row.get("description", ""),
        created_at=row["created_at"],
        split_among=row.get("split_among", []),
        is_settlement=row.get("is_settlement", False),
    )


@app.delete("/api/expenses/{expense_id}")
async def delete_expense(expense_id: str):
    """Delete an expense by its ID."""
    # Verify the expense exists first
    check = (
        supabase.table("expenses")
        .select("id")
        .eq("id", expense_id)
        .execute()
    )
    if not check.data:
        raise HTTPException(status_code=404, detail="Expense not found")

    # Perform the delete (supabase-py may return empty data on success, so don't rely on it)
    supabase.table("expenses").delete().eq("id", expense_id).execute()
    return {"message": "Expense deleted", "id": expense_id}


@app.get("/api/summary", response_model=SummaryOut)
async def get_summary():
    """Calculate total, per-head cost, and individual balances."""
    # Fetch all users
    users_resp = supabase.table("users").select("id, name").order("name").execute()
    users = users_resp.data or []
    user_count = len(users)

    if user_count == 0:
        return SummaryOut(
            total_spent=0, per_head=0, user_count=0, balances=[]
        )

    # Fetch all expenses
    expenses_resp = (
        supabase.table("expenses").select("buyer_id, amount, split_among, is_settlement").execute()
    )
    expenses = expenses_resp.data or []

    # Calculate actual ledger balances
    total_spent = 0.0
    balance_map: dict[str, float] = {u["id"]: 0.0 for u in users}

    for exp in expenses:
        amt = float(exp["amount"])
        bid = exp["buyer_id"]
        is_settlement = exp.get("is_settlement", False)
        split = exp.get("split_among") or []

        # Legacy fallback: if split is empty and it's not a settlement, split among all users
        if len(split) == 0 and not is_settlement:
            split = [u["id"] for u in users]
        
        # Only true food expenses count towards the displayed "Total Spent" bubble
        if not is_settlement:
            total_spent += amt

        # The buyer's ledger balance goes UP
        if bid in balance_map:
            balance_map[bid] += amt

        # Everyone in the split has their ledger balance go DOWN
        if len(split) > 0:
            split_amount = amt / len(split)
            for participant_id in split:
                if participant_id in balance_map:
                    balance_map[participant_id] -= split_amount

    per_head = round(total_spent / user_count, 2) if user_count else 0.0

    # Build balance list
    balances: list[BalanceItem] = []
    for user in users:
        uid = user["id"]
        # In this literal ledger, the positive balance map IS the exact amount they are owed (or negative if they owe)
        final_balance = round(balance_map.get(uid, 0.0), 2)
        total_paid = 0.0 # You can calculate historically paid if needed, but for simplicity we rely on final_balance
        
        status = "Owed" if final_balance >= 0 else "Owes"
        balances.append(
            BalanceItem(
                id=uid,
                name=user["name"],
                total_paid=total_paid,  # deprecated, but kept for model simplicity
                balance=final_balance,
                status=status,
            )
        )

    # Sort: people who are owed first, then those who owe
    balances.sort(key=lambda b: b.balance, reverse=True)

    return SummaryOut(
        total_spent=round(total_spent, 2),
        per_head=per_head,
        user_count=user_count,
        balances=balances,
    )

# ---------------------------------------------------------------------------
# Local Dev: Serve Frontend
# ---------------------------------------------------------------------------
# Mount the public directory so the frontend can be served from the same server
import os
if os.path.exists("public"):
    app.mount("/", StaticFiles(directory="public", html=True), name="public")
