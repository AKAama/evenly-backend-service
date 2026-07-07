from decimal import Decimal
from uuid import UUID
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models import Expense, ExpenseSplit, LedgerMember, User, Settlement, ExpenseStatus


class SettlementCalculator:
    """Calculate settlements using greedy algorithm"""

    def __init__(self, db: Session, ledger_id: UUID):
        self.db = db
        self.ledger_id = ledger_id
        self._members: list[LedgerMember] | None = None

    def get_confirmed_expenses(self) -> list[Expense]:
        """Get all non-rejected expenses for the ledger."""
        return (
            self.db.query(Expense)
            .filter(
                Expense.ledger_id == self.ledger_id,
                Expense.status != ExpenseStatus.REJECTED
            )
            .all()
        )

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
        Calculate net balance for each user.
        net = paid_amount - owed_amount - settled_paid + settled_received

        A recorded settlement A->B of amount X means A has paid X to B,
        which should reduce A's debt by X (A's net += X) and reduce B's
        receivable by X (B's net -= X).

        Positive net: user should receive money
        Negative net: user owes money
        """
        members = self.get_ledger_members()
        member_ids = [m.user_id for m in members]

        # Step 1: Calculate owed amounts (from expense_splits)
        # Each user owes the sum of their splits
        owed_amounts = (
            self.db.query(ExpenseSplit.user_id, func.sum(ExpenseSplit.amount))
            .join(Expense)
            .filter(
                Expense.ledger_id == self.ledger_id,
                Expense.status != ExpenseStatus.REJECTED,
                ExpenseSplit.user_id.in_(member_ids)
            )
            .group_by(ExpenseSplit.user_id)
            .all()
        )

        owed_dict: dict[UUID, Decimal] = {uid: Decimal(str(amount or 0)) for uid, amount in owed_amounts}

        # Step 2: Calculate paid amounts (from expenses as payer)
        paid_amounts = (
            self.db.query(Expense.payer_id, func.sum(Expense.total_amount))
            .filter(
                Expense.ledger_id == self.ledger_id,
                Expense.status != ExpenseStatus.REJECTED,
                Expense.payer_id.in_(member_ids)
            )
            .group_by(Expense.payer_id)
            .all()
        )

        paid_dict: dict[UUID, Decimal] = {uid: Decimal(str(amount or 0)) for uid, amount in paid_amounts}

        # Step 3: Apply already-recorded settlement payments.
        # A->B amount X: A paid X to already settle part of the debt, so
        #   A's net balance increases by X (owes X less)
        #   B's net balance decreases by X (receives X less)
        settlement_paid: dict[UUID, Decimal] = {uid: Decimal("0") for uid in member_ids}
        settlements = (
            self.db.query(Settlement.from_user_id, Settlement.to_user_id, func.sum(Settlement.amount))
            .filter(Settlement.ledger_id == self.ledger_id)
            .group_by(Settlement.from_user_id, Settlement.to_user_id)
            .all()
        )
        for from_uid, to_uid, amount in settlements:
            amt = Decimal(str(amount or 0))
            if from_uid in settlement_paid:
                settlement_paid[from_uid] += amt
            if to_uid in settlement_paid:
                settlement_paid[to_uid] -= amt

        # Step 4: Calculate net balances (receives positive = credits, gives negative = debts)
        net_balances: dict[UUID, Decimal] = {}
        for member in members:
            uid = member.user_id
            owed = owed_dict.get(uid, Decimal("0"))
            paid = paid_dict.get(uid, Decimal("0"))
            settled = settlement_paid.get(uid, Decimal("0"))
            net_balances[uid] = paid - owed + settled

        # Round away tiny floating-point noise
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
                "amount": Decimal
            },
            ...
        ]
        """
        net_balances = self.calculate_net_balances()
        user_names = self.get_user_names()

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
                settlements.append({
                    "from_user_id": debtor_id,
                    "from_user_name": user_names.get(debtor_id, "Unknown"),
                    "to_user_id": creditor_id,
                    "to_user_name": user_names.get(creditor_id, "Unknown"),
                    "amount": transfer_amount,
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
