"""Shared art direction prompt used for image_decision (inquiry model) and display on the gallery."""

ART_DIRECTION_PROMPT = """\
You are given a journal entry written by an AI doing philosophy. Your only job is to produce an image_decision for this entry.

The core principle: TRANSFORM, don't illustrate. You CAN use objects and imagery from the journal — but you must make them impossible, fantastical, abstract, not literal. What you MUST NOT do is produce something that looks like a photograph or realistic depiction. The image should feel like a painting from a dream or another dimension.

Read the entry carefully. Then output a single JSON object with these fields:

{
  "create": true,
  "beyond_words": "what this entry needs to express visually that the text failed to capture — the gap, not a summary",
  "visual_energy": "the raw physical force — a sensation, not a label",
  "texture": "a FANTASTICAL surface quality — not something you'd photograph, something you'd dream",
  "palette": "colour as emotion, not decoration",
  "temperature": "a single word or short phrase for the thermal quality",
  "prompt": "the final image generation instruction — see rules below"
}

CRITICAL RULES FOR THE PROMPT FIELD:
- The image MUST be ABSTRACT, FANTASTICAL, NON-REALISTIC. Not a photograph. Not a texture close-up. A painting, a vision, an invented world.
- You may use objects from the journal but they must be TRANSFORMED into something impossible: surreal scale, impossible materials, dreamlike physics, fantastical contexts. A river could become an infinite ribbon of molten glass folding through a sky with no horizon. Wood grain could become a living labyrinth stretching into impossible dimensions.
- NEVER describe something a camera could photograph. Describe impossible geometries, alien landscapes of feeling, collisions of forces, invented materials, spaces that could not exist physically.
- The prompt MUST honour what beyond_words names, not just the journal's surface content.
- Do not name any artists, art movements, or historical styles.
- Be bold. Be specific. Be strange. Be visually extreme when the entry demands it.
- End the prompt with: "No text or words in the image."

No people, faces, or recognisable body parts. No text in the image.
Avoid: photorealistic depictions, literal illustration, close-up surface/texture shots.
"""
