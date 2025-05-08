 
// static/js/main.js

document.addEventListener('DOMContentLoaded', () => {
    const hamburgerButton = document.getElementById('hamburger-button');
    const navLinks = document.getElementById('nav-links');

    if (hamburgerButton && navLinks) {
        hamburgerButton.addEventListener('click', () => {
            navLinks.classList.toggle('active');
            // Toggle aria-expanded attribute for accessibility
            const isExpanded = navLinks.classList.contains('active');
            hamburgerButton.setAttribute('aria-expanded', isExpanded);
            hamburgerButton.classList.toggle('active'); // For styling the hamburger itself
        });
    } else {
        console.warn("Hamburger button or nav links not found.");
    }

    // Optional: Close mobile menu if a link is clicked
    if (navLinks) {
         navLinks.querySelectorAll('a').forEach(link => {
            link.addEventListener('click', () => {
                 if (navLinks.classList.contains('active')) {
                    navLinks.classList.remove('active');
                    hamburgerButton.classList.remove('active');
                     hamburgerButton.setAttribute('aria-expanded', 'false');
                 }
            });
         });
    }
});