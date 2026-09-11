"""Deterministic English copy for the resume wizard."""

_COPY: dict[str, str] = {
    "intro": "Hi — I'll help you build your master resume. What's your name, and what kind of role are you going for?",
    "contact": "What's the best email, phone, or links (LinkedIn / GitHub / site) to include?",
    "summary": "In a sentence or two, how would you describe yourself professionally?",
    "workExperience": "Tell me about one role: title, company, dates, what you did, and any measurable impact.",
    "internships": "Tell me about one internship: title, company, dates, what you worked on, and what changed because of it.",
    "education": "Tell me about your education: school, degree, dates, and any honors or standout coursework.",
    "personalProjects": "Tell me about one project: what you built, why it mattered, the tech you used, and any results.",
    "skills": "What tools, technologies, or skills do you want on your resume?",
    "review": "Let's review what's here before we create your master resume.",
    "next": "What would you like to add next?",
    "warning_name": "Add your name — it's required to create your resume.",
    "warning_contact": "Add at least one contact method (email, phone, or a link).",
    "warning_experience": "Add at least one experience, internship, or project.",
    "warning_education": "Education is empty — skip only if that's intentional.",
    "warning_skills": "Skills are empty — add tools or technologies you've used.",
}


def wizard_copy(_language: str, key: str) -> str:
    """Return deterministic English wizard copy."""
    return _COPY[key]
