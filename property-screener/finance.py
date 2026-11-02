"""Deterministic BTL screen. Advertisements are never mortgage approvals."""
from datetime import datetime, timezone
import math

DEFAULTS = dict(budget=25000, ltv=.75, rate=.05, stress_rate=.07,
                voids=.05, management=.12, maintenance=.10,
                contingency=.10, insurance=20, compliance=15,
                ready_works=2000, refurb_works=5500, refurb_months=3,
                holding=150, reserve=1500, min_surplus=150,
                ready_price=100000, refurb_price=70000, max_age_days=7)

def settings(values=None):
    out = DEFAULTS | (values or {})
    for k, v in out.items():
        if k not in DEFAULTS or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError('Settings must contain finite numbers only')
        if v < 0: raise ValueError(f'{k} must not be negative')
    for k in ('ltv','rate','stress_rate','voids','management','maintenance','contingency'):
        if out[k] >= 1: raise ValueError(f'{k} must be below 100%')
    if out['ltv'] == 0 or out['rate'] == 0: raise ValueError('LTV and interest rate must be positive')
    if (1-out['voids'])*(1-out['management'])-out['maintenance'] <= 0:
        raise ValueError('Operating assumptions leave no rent available')
    return out

def number(x):
    return isinstance(x, (int,float)) and not isinstance(x,bool) and math.isfinite(x)

def analyse(p, options=None, now=None):
    c=settings(options); now=now or datetime.now(timezone.utc)
    reasons=[]; checks=[]; evidence=p.get('evidence',{})
    price=p.get('price'); rent=p.get('rent'); beds=p.get('beds')
    route=p.get('route','unknown'); financial=None
    if route not in ('ready','refurb'): checks.append('Confirm condition / works route')
    if p.get('country') != 'England':
        (reasons if p.get('country') else checks).append('Outside England' if p.get('country') else 'Confirm England location')
    if beds not in (2,3):
        (reasons if beds is not None else checks).append('Requires 2–3 bedrooms')
    if p.get('listed') == 'listed': reasons.append('Listed building excluded')
    elif p.get('listed') != 'unlisted' or not evidence.get('listed'): checks.append('Confirm unlisted status for exact building')
    if p.get('sale_status') in ('sold','sstc','withdrawn'): reasons.append('Sold / withdrawn from active search')
    elif p.get('sale_status') != 'available': checks.append('Confirm current sale availability')
    if p.get('cash_only'): reasons.append('Cash-buyers-only restriction')
    if p.get('mortgage') == 'rejected': reasons.append('Mortgage rejected')
    elif p.get('mortgage') != 'confirmed' or not evidence.get('mortgage'): checks.append('Broker must confirm mortgage and timetable')
    if p.get('auction'): checks.append('Auction guide/reserve, premium and completion terms')
    if p.get('tenure') not in ('freehold','leasehold') or not evidence.get('title'): checks.append('Confirm tenure, title and letting restrictions')
    if p.get('tenure') == 'leasehold' and not evidence.get('lease'): checks.append('Confirm lease term and rent-review clauses')
    try:
        age=(now-datetime.fromisoformat(p['observed_at'].replace('Z','+00:00'))).total_seconds()/86400
        if age < -.01 or age > c['max_age_days']: checks.append('Advert data is stale / has invalid date')
    except (KeyError,ValueError,TypeError): checks.append('No successful advert observation date')
    if not number(price) or price <= 0: checks.append('Purchase price missing or conflicting')
    elif price > (c['refurb_price'] if route=='refurb' else c['ready_price']): reasons.append('Purchase-price ceiling exceeded')
    if not number(rent) or rent <= 0: checks.append('Rent missing: no cashflow result')
    if not evidence.get('rent'): checks.append('Verify rent / tenancy ledger or rental appraisal')
    works=p.get('works')
    if not number(works): works=c['refurb_works'] if route=='refurb' else c['ready_works']
    if not evidence.get('works'): checks.append('Works are an allowance, not a quote')
    charges=p.get('annual_charges')
    if not number(charges): checks.append('Annual lease charges unknown: no cashflow result')
    if not evidence.get('costs'): checks.append('Verify licence, insurance and annual charges')
    months=p.get('vacant_months',c['refurb_months'] if route=='refurb' else 0)
    if not number(months) or months<0: checks.append('Holding period invalid'); months=0
    if number(price) and price>0:
        loan=price*c['ltv']; interest=loan*c['rate']/12
        deposit=price-loan; contingency=works*c['contingency']
        holding=months*(interest+c['holding'])
        initial=deposit+works+contingency+holding; headroom=c['budget']-initial
        financial=dict(price=price,rent=rent,works=works,contingency=contingency,
                       loan=loan,deposit=deposit,interest=interest,holding=holding,
                       initial=initial,headroom=headroom,reserve_headroom=headroom-c['reserve'],
                       cashflow=None,stress_cashflow=None,break_even=None,max_cashflow_price=None,
                       max_budget_price=(c['budget']-works-contingency-months*c['holding'])/
                       ((1-c['ltv'])+months*c['ltv']*c['rate']/12),
                       max_base_works=(c['budget']-deposit-holding)/(1+c['contingency']))
        if headroom<0: reasons.append('Deposit, works and holding costs exceed £25k pot')
        if number(rent) and rent>0 and number(charges):
            available=rent*((1-c['voids'])*(1-c['management'])-c['maintenance'])
            fixed=c['insurance']+c['compliance']+charges/12
            cash=available-fixed-interest
            financial.update(cashflow=cash,stress_cashflow=cash-loan*(c['stress_rate']-c['rate'])/12,
                             break_even=(fixed+interest)/((1-c['voids'])*(1-c['management'])-c['maintenance']),
                             max_cashflow_price=(available-fixed-c['min_surplus'])*12/(c['ltv']*c['rate']),
                             gross_yield=rent*12/price)
            if cash <= 0: reasons.append('No positive pre-tax monthly surplus')
    if reasons: status='excluded'
    elif financial and financial['cashflow'] is not None and not checks: status='verified'
    elif financial and financial['cashflow'] is not None: status='model_fits'
    else: status='needs_data'
    # Sorting is an investigation priority, not an investment score/valuation.
    priority=(financial.get('cashflow') or 0) if financial else 0
    if financial and financial['reserve_headroom'] < 0: priority-=75
    if p.get('rent_basis') != 'Advertised current tenancy': priority-=35
    if p.get('auction'): priority-=50
    if number(price) and price<65000: checks.append('Low-value lender eligibility check'); priority-=50
    if status=='verified' and checks: status='model_fits'
    return dict(status=status,reasons=reasons,checks=list(dict.fromkeys(checks)),financial=financial,priority=priority)
