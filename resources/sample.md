# Beispielkurs

Willkommen im Beispielkurs. Diese Seite zeigt alle unterstützten Komponenten.

## Text

### Was ist Markdown?

Alles in einer Textkomponente wird als Markdown gerendert.

- *Kursiv*, **fett**, `Code`
- > Blockzitate funktionieren ebenfalls
- Links wie [dieser hier](https://www.markdownguide.org/) werden unterstützt

## MCQ

### Wie viele Kontinente gibt es auf der Erde?

* [ ] 5
* [x] 7
* [ ] 9

### Welche der folgenden Sprachen ist objektorientiert? (mit Feedback)

* [ ] HTML
* [x] Python
* [ ] CSS

feedback: HTML und CSS sind Auszeichnungssprachen, keine Programmiersprachen. Python ist objektorientiert.

## Slider

### Wie sicher fühlen Sie sich im Umgang mit diesem Thema?

scale: 1..10
labelStart: "1 = gar nicht sicher"
labelEnd: "10 = sehr sicher"

## Matching

### Ordnen Sie die Hauptstädte den Ländern zu.
instruction: Wählen Sie die richtige Hauptstadt für jedes Land.
_isRandom: False
_isRandomQuestionOrder: True

- Frankreich
  - [x] Paris
  - [ ] Rom
  - [ ] Berlin

- Deutschland
  - [ ] Paris
  - [ ] Rom
  - [x] Berlin

- Italien
  - [ ] Paris
  - [x] Rom
  - [ ] Berlin

## Reflexion

### Wie würden Sie das Gelernte in Ihrem Alltag anwenden?

placeholder: "Schreiben Sie Ihre Gedanken hier..."
feedback: Vielen Dank für Ihre Reflexion!

## Akkordeon

### Häufig gestellte Fragen

**Was ist der Unterschied zwischen Text und HTML?**
Text ist reiner Inhalt ohne Formatierung. HTML fügt Struktur und Bedeutung hinzu, z. B. Überschriften, Absätze und Links.

**Warum ist Feedback bei Fragen wichtig?**
Feedback hilft Lernenden, ihre Antworten einzuordnen und aus Fehlern zu lernen.

**Kann ich mehrere Komponenten auf einer Seite kombinieren?**
Ja – jeder Abschnitt mit einer `###`-Überschrift wird als eigene Komponente erkannt und entsprechend dargestellt.
