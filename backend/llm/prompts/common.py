"""Facts shared by several prompts (kept in one place so synthesis and verification agree)."""

from backend.data_gen.reference import AS_OF

REFERENCE_FACTS = f"""HealthGuard reference facts (always accurate; may be used without a source tag):
- All member data is current through {AS_OF.strftime("%b %-d, %Y")}; treat that as today's date when reasoning about deadlines and elapsed time.
- Member Services: 1-800-446-3623
- 24/7 Nurse Advice Line: 1-800-446-8773
- Behavioral Health Line (24/7): 1-800-446-3682
- 988 Suicide & Crisis Lifeline (call or text 988); 911 for emergencies"""
