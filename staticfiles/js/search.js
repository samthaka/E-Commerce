
    // static/js/search.js
    $(document).ready(function() {
        // When the user types in the search bar
        $('#search-bar').keyup(function() {
            let query = $(this).val();
            if (query.length > 0) {
                $.ajax({
                    url: '/search/',
                    data: {'query': query},
                    success: function(data) {
                        $('#search-popup').html(data);  // Update the popup with the results
                        $('#search-popup').show();  // Show the popup
                    }
                });
            } else {
                $('#search-popup').hide();  // Hide the popup when input is empty
            }
        });
    });
    