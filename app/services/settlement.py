from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func

from app.models import Expense, ExpenseSplit, LedgerMember, User, Settlement, ExpenseStatus

CENT = Decimal("0.01")


def normalize_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def expense_refund_amount(expense) -> Decimal:
    return normalize_money(Decimal(str(getattr(expense, "refund_amount", 0) or 0)))


def expense_net_amount(expense) -> Decimal:
    """Effective spend after partial refunds (what settlement should use)."""
    total = normalize_money(Decimal(str(expense.total_amount)))
    return normalize_money(total - expense_refund_amount(expense))


def expense_scaled_split_amounts(expense) -> list[tuple]:
    """Return (split, effective_amount) scaled so sum equals net amount."""
    total = normalize_money(Decimal(str(expense.total_amount)))
    net = expense_net_amount(expense)
    splits = list(expense.splits or [])
    if not splits:
        return []
    if expense_refund_amount(expense) == 0 or total == 0:
        return [(s, normalize_money(Decimal(str(s.amount)))) for s in splits]

    # Largest-remainder on cents so scaled shares sum exactly to net.
    ratio = net / total
    raw_cents = [Decimal(str(s.amount)) * ratio * 100 for s in splits]
    floors = [int(c) for c in raw_cents]  # truncate toward zero
    remainder = int(net * 100) - sum(floors)
    order = sorted(
        range(len(raw_cents)),
        key=lambda i: raw_cents[i] - floors[i],
        reverse=True,
    )
    for i in range(max(remainder, 0)):
        floors[order[i % len(order)]] += 1
    while sum(floors) > int(net * 100) and any(floors):
        idx = max(range(len(floors)), key=lambda i: floors[i])
        if floors[idx] <= 0:
            break
        floors[idx] -= 1

    return [
        (s, normalize_money(Decimal(floors[i]) / 100))
        for i, s in enumerate(splits)
    ]


class SettlementCalculator:
    """Calculate settlements using greedy algorithm"""

    def __init__(self, db: Session, ledger_id: UUID):
        self.db = db
        self.ledger_id = ledger_id
        self._members: list[LedgerMember] | None = None

    def get_confirmed_expenses(self) -> list[Expense]:
        """Expenses fully confirmed (legacy helper)."""
        return (
            self.db.query(Expense)
            .options(joinedload(Expense.splits))
            .filter(
                Expense.ledger_id == self.ledger_id,
                Expense.status == ExpenseStatus.CONFIRMED,
            )
            .all()
        )

    def get_settlement_expenses(self) -> list[Expense]:
        """Expenses that shape transfer flow: confirmed + pending (not rejected).

        Pending bills are projected into the same final flow so amounts stay
        stable as confirmations complete; clients mark affected rows as unconfirmed.
        """
        return (
            self.db.query(Expense)
            .options(joinedload(Expense.splits))
            .filter(
                Expense.ledger_id == self.ledger_id,
                Expense.status.in_([ExpenseStatus.CONFIRMED, ExpenseStatus.PENDING]),
            )
            .all()
        )

    def users_involved_in_pending_expenses(self) -> set[UUID]:
        """Registered users on any still-pending bill (payer or split)."""
        involved: set[UUID] = set()
        for expense in self.get_settlement_expenses():
            if expense.status != ExpenseStatus.PENDING:
                continue
            if expense.payer_id is not None:
                involved.add(expense.payer_id)
            for split in expense.splits or []:
                if split.user_id is not None:
                    involved.add(split.user_id)
        return involved

    def get_ledger_members(self) -> list[LedgerMember]:
        """Get active members of the ledger (pending invitations don't participate in balances)"""
        if self._members is None:
            self._members = (
                self.db.query(LedgerMember)
                .filter(
                    LedgerMember.ledger_id == self.ledger_id,
                    LedgerMember.status == "active",
                )
                .all()
            )
        return self._members

    def calculate_net_balances(self) -> dict[UUID, Decimal]:
        """
        Calculate net balance for each registered user.

        Net contribution = paid - owed (payer paid total, each owes split).

        Positive net: user should receive money
        Negative net: user owes money

        Includes pending bills so the projected transfer flow matches the
        fully-confirmed end state.
        """
        members = self.get_ledger_members()
        member_ids = {m.user_id for m in members if m.user_id is not None}
        net_balances: dict[UUID, Decimal] = {uid: Decimal("0") for uid in member_ids}

        for expense in self.get_settlement_expenses():
            if expense.payer_id in net_balances:
                net_balances[expense.payer_id] += expense_net_amount(expense)
            for split, amount in expense_scaled_split_amounts(expense):
                if split.user_id is not None and split.user_id in net_balances:
                    net_balances[split.user_id] -= amount

        for uid in list(net_balances.keys()):
            if abs(net_balances[uid]) < Decimal("0.01"):
                net_balances[uid] = Decimal("0")

        return net_balances

    def get_user_names(self) -> dict[UUID, str]:
        """Get display names for all ledger members"""
        members = self.get_ledger_members()
        user_ids = [m.user_id for m in members]
        users = self.db.query(User).filter(User.id.in_(user_ids)).all()
        return {u.id: u.display_name or u.email for u in users}

    def calculate_settlements(self) -> list[dict]:
        """
        Calculate optimal settlement instructions using greedy algorithm.

        Returns list of settlements:
        [
            {
                "from_user_id": UUID,
                "to_user_id": UUID,
                "amount": Decimal,
                "includes_unconfirmed": bool,
            },
            ...
        ]
        """
        net_balances = self.calculate_net_balances()
        user_names = self.get_user_names()
        pending_users = self.users_involved_in_pending_expenses()

        # Separate into creditors (positive balance) and debtors (negative balance)
        creditors: list[tuple[UUID, Decimal]] = []
        debtors: list[tuple[UUID, Decimal]] = []

        for user_id, balance in net_balances.items():
            if balance > 0:
                creditors.append((user_id, balance))
            elif balance < 0:
                debtors.append((user_id, -balance))  # Store as positive for easier calculation

        # Sort by amount (descending) for optimal settlement
        creditors.sort(key=lambda x: x[1], reverse=True)
        debtors.sort(key=lambda x: x[1], reverse=True)

        settlements: list[dict] = []

        # Greedy algorithm
        i, j = 0, 0
        while i < len(creditors) and j < len(debtors):
            creditor_id, creditor_amount = creditors[i]
            debtor_id, debtor_amount = debtors[j]

            # Transfer amount is minimum of what creditor is owed and what debtor owes
            transfer_amount = min(creditor_amount, debtor_amount)

            if transfer_amount > 0:
                # Gray / unconfirmed when either side still has open pending bills.
                includes_unconfirmed = (
                    debtor_id in pending_users or creditor_id in pending_users
                )
                settlements.append({
                    "from_user_id": debtor_id,
                    "from_user_name": user_names.get(debtor_id, "Unknown"),
                    "to_user_id": creditor_id,
                    "to_user_name": user_names.get(creditor_id, "Unknown"),
                    "amount": transfer_amount,
                    "includes_unconfirmed": includes_unconfirmed,
                })

            # Update balances
            creditors[i] = (creditor_id, creditor_amount - transfer_amount)
            debtors[j] = (debtor_id, debtor_amount - transfer_amount)

            # Move to next if settled
            if creditors[i][1] <= 0:
                i += 1
            if debtors[j][1] <= 0:
                j += 1

        return settlements


def create_settlement_record(
    db: Session,
    ledger_id: UUID,
    from_user_id: UUID,
    to_user_id: UUID,
    amount: Decimal,
    note: str | None = None
) -> Settlement:
    """Create a settlement record in the database"""
    settlement = Settlement(
        ledger_id=ledger_id,
        from_user_id=from_user_id,
        to_user_id=to_user_id,
        amount=amount,
        note=note,
    )
    db.add(settlement)
    db.commit()
    db.refresh(settlement)
    return settlement
